#!/usr/bin/env python3
#
# app/errors.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Internal error hierarchy; outward text is derived later from the error code."""


class DeviceApiError(Exception):
    """Base class. Carries only a machine-readable code, never outward text."""

    code: str = "internal_error"

    def __init__(self, code: str | None = None, **context: object) -> None:
        self.code = code or type(self).code
        self.context = context
        super().__init__(self.code)


class DeviceUnreachable(DeviceApiError):
    code = "device_unreachable"


class DeviceTimeout(DeviceApiError):
    code = "device_timeout"


class ProtocolError(DeviceApiError):
    code = "protocol_error"


class DecodeLengthMismatch(ProtocolError):
    code = "decode_length_mismatch"

    def __init__(self, expected: int, received: int, **context: object) -> None:
        super().__init__(expected=expected, received=received, **context)
        self.expected = expected
        self.received = received


class WriteOutcomeUnknown(DeviceApiError):
    code = "write_outcome_unknown"


class ActionOutcomeUnknown(DeviceApiError):
    code = "action_outcome_unknown"


class DeviceMaintenance(DeviceApiError):
    code = "device_maintenance"


class QueueFullError(DeviceApiError):
    code = "queue_full"


class QueueTimeout(DeviceApiError):
    code = "queue_timeout"


class BudgetExhausted(DeviceApiError):
    code = "budget_exhausted"


class ConfigError(DeviceApiError):
    code = "config_error"


class UnknownDevice(DeviceApiError):
    code = "unknown_device"


class UnknownMetric(DeviceApiError):
    code = "unknown_metric"


class FreshNotAvailable(DeviceApiError):
    code = "fresh_not_available_for_periodic_metric"


class WriteRejected(DeviceApiError):
    """A write or action was refused before any transaction; ``code`` carries the problem key.

    Codes: write_not_allowed, metric_is_action, value_out_of_range, value_type_mismatch,
    value_not_finite, value_step_mismatch.
    """

    code = "write_not_allowed"


class AuthenticationError(DeviceApiError):
    """Codes: missing_token, invalid_token (both 401)."""

    code = "invalid_token"


class InsufficientScope(DeviceApiError):
    code = "insufficient_scope"


class RateLimited(DeviceApiError):
    """Caller or auth-failure limit hit; ``retry_after`` (seconds) is carried in the context."""

    code = "rate_limited"


class WriteDisabled(DeviceApiError):
    code = "write_disabled"


class NotFound(DeviceApiError):
    code = "not_found"


class ReconfigurationBuildError(Exception):
    """The new device graph could not be built; the old graph is still intact and running."""
