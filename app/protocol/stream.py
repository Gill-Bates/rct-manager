#!/usr/bin/env python3
#
# app/protocol/stream.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Incremental frame extraction from a TCP byte stream. Stateful, I/O-free."""

import logging
from dataclasses import dataclass

from app.errors import ProtocolError
from app.protocol.frames import CrcMismatch, Frame, decode_frame
from app.protocol.types import (
    BOOTLOADER_MAGIC,
    LONG_COMMANDS,
    START_BYTE,
    STOP_BYTE,
    Command,
)

log = logging.getLogger(__name__)

_VALID_COMMANDS = frozenset(int(c) for c in Command)


@dataclass(slots=True)
class ParseStats:
    discarded_bytes: int = 0
    crc_errors: int = 0
    framing_errors: int = 0
    oversize_frames: int = 0
    # Long frames whose frame end had to be taken from the framing because the declared length
    # did not match it (Requirement 2.13, 2.14).
    length_field_corrected: int = 0
    # Command bytes the protocol does not define. Counted here and rate-limited by the receiver,
    # which owns the clock (Requirement 1.12).
    unknown_commands: int = 0
    last_unknown_command: int | None = None


def _should_log(count: int) -> bool:
    """Sample per connection: the first few occurrences plus powers of two.

    A peer sending invalid material continuously would otherwise log at network rate.
    """
    return count <= 3 or count & (count - 1) == 0


class _Incomplete(Exception):
    """More bytes are needed to finish the frame."""


class _Resync(Exception):
    """Drop the current frame; the next frame may begin at raw offset ``pos``."""

    def __init__(self, pos: int, *, framing: bool = False, oversize: bool = False) -> None:
        super().__init__(pos)
        self.pos = pos
        self.framing = framing
        self.oversize = oversize


class StreamParser:
    """Reassembles frames across reads and resynchronizes after line noise."""

    def __init__(self, *, max_frame_bytes: int = 4096) -> None:
        self._max = max_frame_bytes
        self._buf = bytearray()
        self._stats = ParseStats()
        self._tail = b""
        self._magic = False

    @property
    def stats(self) -> ParseStats:
        return self._stats

    @property
    def buffered_bytes(self) -> int:
        return len(self._buf)

    @property
    def bootloader_magic_seen(self) -> bool:
        return self._magic

    def feed(self, chunk: bytes) -> list[Frame]:
        """Append bytes and return all frames completed by them, in receive order."""
        self._buf += chunk
        frames: list[Frame] = []
        while True:
            start = self._buf.find(START_BYTE)
            if start < 0:
                # A lone trailing null byte may precede the next start byte.
                keep_null = bool(self._buf) and self._buf[-1] == 0
                drop = len(self._buf) - (1 if keep_null else 0)
                self._scan_unframed(bytes(self._buf[:drop]))
                self._discard(drop)
                return frames
            lead = start - 1 if start > 0 and self._buf[start - 1] == 0 else start
            if lead:
                self._scan_unframed(bytes(self._buf[:lead]))
            self._tail = b""  # a frame candidate separates two stretches of unframed bytes
            self._discard(lead)
            try:
                frame, end = self._extract(1 + (start - lead))
            except _Incomplete:
                return frames
            except _Resync as exc:
                if exc.framing:
                    self._stats.framing_errors += 1
                if exc.oversize:
                    self._stats.oversize_frames += 1
                self._discard(max(exc.pos, 1))
                continue
            if frame is not None:
                frames.append(frame)
            del self._buf[:end]

    def _scan_unframed(self, data: bytes) -> None:
        """Search the magic only outside frames; a valid payload may carry the same byte sequence."""
        if not data:
            return
        window = self._tail + data
        if BOOTLOADER_MAGIC in window:
            self._magic = True
        self._tail = window[-(len(BOOTLOADER_MAGIC) - 1) :]

    def _discard(self, count: int) -> None:
        if count > 0:
            self._stats.discarded_bytes += count
            del self._buf[:count]

    def _take(self, pos: int, count: int) -> tuple[bytes, int]:
        """Read ``count`` unescaped bytes from raw offset ``pos``; return them and the new offset."""
        out = bytearray()
        buf = self._buf
        while len(out) < count:
            if pos >= len(buf):
                raise _Incomplete
            byte = buf[pos]
            if byte == START_BYTE:
                raise _Resync(pos)
            if byte == STOP_BYTE:
                if pos + 1 >= len(buf):
                    raise _Incomplete
                nxt = buf[pos + 1]
                if nxt not in (START_BYTE, STOP_BYTE):
                    raise _Resync(pos + 1, framing=True)
                out.append(nxt)
                pos += 2
            else:
                out.append(byte)
                pos += 1
        return bytes(out), pos

    def _scan_to_next_start(self, pos: int) -> int:
        """Raw offset of the next unmasked start byte at or after ``pos``, carrying the escape state.

        A ``0x2B`` that is the data byte of a ``0x2D 0x2B`` pair is skipped, so it never passes as
        the beginning of a frame (Requirement 2.15). Raises ``_Incomplete`` while the stream may
        still deliver such a byte, and ``_Resync(..., oversize=True)`` once the scan has walked
        ``max_frame_bytes`` without finding one.
        """
        buf = self._buf
        bound = pos + self._max
        while pos < len(buf) and pos < bound:
            byte = buf[pos]
            if byte == START_BYTE:
                return pos
            if byte == STOP_BYTE:
                if pos + 1 >= len(buf):
                    raise _Incomplete
                nxt = buf[pos + 1]
                if nxt not in (START_BYTE, STOP_BYTE):
                    # Same framing rule as _take(): an escape pair the normal decoder would
                    # reject must not be tolerated here either.
                    raise _Resync(pos + 1, framing=True)
                pos += 2
            else:
                pos += 1
        if pos >= bound:
            raise _Resync(pos, oversize=True)
        raise _Incomplete

    def _ends_inside_escape(self, pos: int) -> bool:
        """True when the buffer ends between a stop byte and its data byte, walking from ``pos``.

        Looking only at the last byte is wrong: ``0x2D 0x2D`` is a complete pair that ends in a
        stop byte, and a CRC low byte of 0x2D produces exactly that at the end of a frame.
        """
        buf = self._buf
        while pos < len(buf):
            pos += 2 if buf[pos] == STOP_BYTE else 1
        return pos > len(buf)

    def _unescape_until(self, pos: int, raw_end: int) -> bytes:
        """Unescape the raw bytes from ``pos`` up to ``raw_end``, which holds no unmasked start byte."""
        out = bytearray()
        buf = self._buf
        while pos < raw_end:
            if buf[pos] == STOP_BYTE:
                out.append(buf[pos + 1])
                pos += 2
            else:
                out.append(buf[pos])
                pos += 1
        return bytes(out)

    def _recover_long_frame(
        self, header: bytes, pos: int, declared: int, *, min_end: int = 0, wait: bool = True
    ) -> tuple[Frame, int] | None:
        """Take the frame end of a long frame from the framing instead of from the length field.

        Measured on an RCT Power DC 10.0, firmware 2.3.5687: 16 of 18 long responses declare a
        length that does not match the frame end (14 of them 16 bytes too small, 2 of them 472 and
        480 bytes too large), while 278 of 278 short responses declare it correctly - which is why
        only long commands take this path and the short path stays bit-for-bit as it was.

        At most two candidate boundaries are tried: before a trailing run of ``0x00`` separator
        bytes, then one byte into that run in case the low CRC byte is itself ``0x00``. That order
        matters because appending a zero byte to a valid CRC drives the register to zero, so a
        separator could otherwise slip in as payload. Trying the shorter candidate first resolves
        the ambiguity in favour of the true frame; a wrong shorter boundary fails the CRC and falls
        back. With two candidates the chance of accepting a wrong boundary stays at or below
        2 * 2**-16 per frame, far below a backwards search over many lengths. The received
        length bytes stay in the CRC input. When no subsequent start byte is buffered yet, the
        current end of the buffer is tried as a provisional boundary instead: accepted only if it
        verifies, otherwise ``_Incomplete`` is raised so the caller waits for more data rather than
        giving up on a long response whose next frame never arrives on the same connection. Returns
        ``None`` when a real next-start boundary exists but no candidate verifies; the caller then
        keeps today's behaviour. ``min_end`` restricts the candidates to ends beyond a frame the
        declared length already verified, and ``wait=False`` returns ``None`` instead of waiting
        for more bytes; both serve ``_prefer_outer_boundary``.
        """
        provisional = False
        try:
            boundary = self._scan_to_next_start(pos)
        except _Incomplete:
            boundary = len(self._buf)
            provisional = True
            if self._ends_inside_escape(pos):
                # A lone escape byte at the current buffer end has no pair yet; its candidate
                # body cannot be unescaped until the next byte arrives.
                if not wait:
                    return None
                raise _Incomplete from None
        except _Resync as exc:
            # The caller increments the counters while handling _Resync, so the sampling decision
            # has to look one occurrence ahead.
            if exc.oversize and _should_log(self._stats.oversize_frames + 1):
                log.warning(
                    "Discarding oversize frame without a frame end in sight: "
                    "command=0x%02x declared_length=%d",
                    header[0],
                    declared,
                )
            elif exc.framing and _should_log(self._stats.framing_errors + 1):
                log.debug(
                    "Discarding frame with invalid escaping: command=0x%02x declared_length=%d",
                    header[0],
                    declared,
                )
            raise
        zero_start = boundary
        while zero_start > pos and self._buf[zero_start - 1] == 0:
            zero_start -= 1
        candidates = [zero_start, zero_start + 1] if zero_start < boundary else [boundary]
        for end in candidates:
            if end <= min_end:
                continue
            body = self._unescape_until(pos, end)
            if len(body) < 2:
                continue
            try:
                frame = decode_frame(header + body, measured_length=True)
            except ProtocolError:
                continue
            self._stats.length_field_corrected += 1
            measured = len(body) - 2  # comparable to the declared length: [address,] id and data
            log.debug(
                "Corrected length field: command=0x%02x declared_length=%d measured_length=%d object_id=0x%08x",
                header[0],
                declared,
                measured,
                frame.object_id,
            )
            return frame, end
        if provisional and wait:
            raise _Incomplete
        return None

    def _prefer_outer_boundary(
        self, header: bytes, pos: int, declared: int, frame: Frame, end: int
    ) -> tuple[Frame, int]:
        """Let the outer frame boundary win over a CRC match that an under-declared length produced.

        A long frame whose declared length verifies but leaves bytes before the next start byte
        (besides one separator null) may be a shortened frame whose truncated body collided with
        the CRC (chance 2**-16). The full region is checked first; the declared frame stands when
        that does not verify too, so noise after a genuine frame stays harmless.
        """
        buf = self._buf
        tail = buf[end:]
        if not tail or tail[0] == START_BYTE or (tail[0] == 0 and (len(tail) == 1 or tail[1] == START_BYTE)):
            return frame, end  # nothing beyond the declared end, or only a separator
        try:
            return self._recover_long_frame(header, pos, declared, min_end=end, wait=False) or (frame, end)
        except _Resync:
            return frame, end  # no outer boundary in reach: the verified declared frame stands

    def _resync_after_discard(self, body_start: int, candidate_end: int) -> None:
        """Drop a frame with a valid head escape-aware, so a masked 0x2B inside it starts no frame.

        Requirement 2.6 (the raw search before the FIRST start byte) and 2.7 (an unmasked 0x2B
        begins a new frame) are untouched: here the escape state from the frame start is known.
        ``candidate_end`` is the escape-aware end of the already fully taken frame body (the
        ``end`` from ``_take(pos, length + 2)``), not the raw offset after command and length -
        a scan that cannot find a real next start byte must not fall back before that point, or a
        masked start byte still inside the discarded frame could be treated as the start of a new
        one.
        """
        try:
            end = self._scan_to_next_start(body_start)
        except (_Incomplete, _Resync):
            end = candidate_end
        if end > candidate_end and self._buf[end - 1] == 0:
            end -= 1  # a lone separator null belongs to no frame and is not counted
        self._discard(end)

    def _extract(self, body_start: int) -> tuple[Frame | None, int]:
        """Parse one frame whose start byte sits at offset ``body_start - 1``."""
        head, pos = self._take(body_start, 1)
        command = head[0]
        if command not in _VALID_COMMANDS:
            self._stats.unknown_commands += 1
            self._stats.last_unknown_command = command
            if _should_log(self._stats.unknown_commands):
                log.debug("Discarding frame with unknown command byte 0x%02x", command)
            raise _Resync(body_start)
        width = 2 if command in LONG_COMMANDS else 1
        raw_len, pos = self._take(pos, width)
        length = int.from_bytes(raw_len, "big")
        if 1 + width + length + 2 > self._max:
            if _should_log(self._stats.oversize_frames + 1):  # counted by the caller on _Resync
                log.warning("Discarding oversize frame: command=0x%02x declared_length=%d", command, length)
            raise _Resync(body_start, oversize=True)
        header = head + raw_len
        try:
            rest, end = self._take(pos, length + 2)
        except (_Incomplete, _Resync):
            # The declared length reaches past an unmasked start byte or past the buffered bytes.
            # Measured: two long responses declared 488 bytes and carried 16 and 8, with no further
            # frame behind them. The recovery accepts the buffer end only if the CRC verifies, so a
            # correct frame still arriving in chunks keeps waiting (_Incomplete is re-raised).
            if command not in LONG_COMMANDS:
                raise
            recovered = self._recover_long_frame(header, pos, length)
            if recovered is None:
                raise
            return recovered
        try:
            frame = decode_frame(header + rest)
            if command in LONG_COMMANDS:
                return self._prefer_outer_boundary(header, pos, length, frame, end)
            return frame, end
        except ProtocolError as exc:
            if isinstance(exc, CrcMismatch):
                if command in LONG_COMMANDS:
                    recovered = self._recover_long_frame(header, pos, length)
                    if recovered is not None:
                        return recovered
                self._stats.crc_errors += 1
                if _should_log(self._stats.crc_errors):
                    log.warning(
                        "Discarding frame with CRC mismatch: command=0x%02x declared_length=%d "
                        "object_id=0x%08x expected_crc=0x%04x received_crc=0x%04x",
                        command,
                        length,
                        exc.object_id,
                        exc.expected,
                        exc.received,
                    )
            else:
                self._stats.framing_errors += 1
            self._resync_after_discard(body_start, end)
            return None, 0
