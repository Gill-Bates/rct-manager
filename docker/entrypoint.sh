#!/usr/bin/env bash
#
# docker/entrypoint.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

set -euo pipefail

# Configuration
readonly DATA_DIR="${DATA_DIR:-/app/data}"
# Fixed, not env-driven: the image creates exactly this account (uid/gid 10001)
# and an operator-supplied name must not choose who the server runs as.
readonly APP_USER="rctapi"

log() {
    # stderr: log()/fail() output must never land on a caller's stdout.
    printf '[%s] [%s] %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "entrypoint" "$*" >&2
}

fail() {
    log "ERROR: $*"
    exit 1
}

# DATA_DIR is operator-supplied and reaches a recursive chown that runs as
# root, so a value like DATA_DIR=/ or /etc would rewrite ownership across the
# container, and across the host for a bind mount. Reject anything that is not
# an absolute path outside the system directories before that can happen.
validate_managed_dir() {
    local name="$1"
    local path="$2"

    [[ "${path}" == /* ]] || fail "${name} must be an absolute path, got: ${path}"

    local resolved
    resolved="$(realpath -m -- "${path}")" || fail "cannot resolve ${name}: ${path}"

    # Component-boundary match, not a substring/exact check: a bare case match
    # on "/etc" let "/etc/rct" or "/etc-staging" through unblocked, and either
    # one still reaches the recursive chown below. /app and / are blocked only
    # as exact paths: /app/data (the default DATA_DIR) legitimately lives
    # under /app, and stripping /'s own trailing slash would otherwise leave
    # an empty prefix that matches every absolute path. Every other entry
    # blocks itself and its whole subtree.
    [[ "${resolved}" == "/" || "${resolved}" == "/app" ]] \
        && fail "refusing to use ${resolved} as ${name}: chown would damage the system"

    local forbidden
    for forbidden in /bin /boot /dev /etc /home /lib /lib64 /media /mnt /opt /proc /root /run /sbin /srv /sys /tmp /usr /var; do
        if [[ "${resolved}" == "${forbidden}" || "${resolved}" == "${forbidden}/"* ]]; then
            fail "refusing to use ${resolved} as ${name}: chown would damage the system"
        fi
    done
}

# The capability set is deliberately minimal (CHOWN, SETUID, SETGID), so root
# has no DAC_OVERRIDE/FOWNER: everything that needs the app user's ownership
# (mode, write test) runs as that user via gosu, after the chown.
bootstrap_as_root() {
    command -v gosu >/dev/null 2>&1 || fail "gosu not found in PATH"
    id "${APP_USER}" >/dev/null 2>&1 || fail "user ${APP_USER} does not exist"

    local app_uid
    app_uid="$(id -u "${APP_USER}")"

    mkdir -p -- "${DATA_DIR}" || fail "cannot create ${DATA_DIR}"

    # Skipped once the top directory has the right owner (also the case for a
    # fresh named volume, which inherits it from the image), so a large tree is
    # not walked on every restart. Children first (-depth): after the top
    # directory is handed over, root can no longer enter a 0700 directory.
    if [[ "$(stat -c %u -- "${DATA_DIR}")" != "${app_uid}" ]]; then
        log "Repairing ownership of ${DATA_DIR} for ${APP_USER} ..."
        repair_ownership "${app_uid}"
    elif find -P "${DATA_DIR}" -xdev \! -user "${app_uid}" -print -quit 2>/dev/null | grep -q .; then
        # The top directory is correctly owned (so the fast path above was
        # skipped) but something below it is not - e.g. a restored file from a
        # backup made outside the container. Still needs the full repair.
        log "Repairing ownership below ${DATA_DIR} for ${APP_USER} (top-level owner was already correct) ..."
        repair_ownership "${app_uid}"
    fi

    gosu "${APP_USER}" chmod 0700 -- "${DATA_DIR}" 2>/dev/null \
        || log "WARNING: cannot set mode 0700 on ${DATA_DIR}"
    gosu "${APP_USER}" test -d "${DATA_DIR}" -a -w "${DATA_DIR}" || fail_not_writable

    # Ownership alone does not guarantee access: a restored file can carry
    # permission bits (not just an owner) that lock the app user out even
    # though the top-level directory is fine. This probe is read-only (no
    # chown), so it costs one tree walk, not a tree rewrite.
    local inaccessible
    inaccessible="$(gosu "${APP_USER}" find -P "${DATA_DIR}" -xdev \! -readable -print -quit 2>/dev/null)"
    if [[ -n "${inaccessible}" ]]; then
        ls -ld -- "${inaccessible}" 2>/dev/null || true
        fail "${inaccessible} is not readable by ${APP_USER} even after the ownership repair; fix its permissions on the host"
    fi
}

repair_ownership() {
    local app_uid="$1"
    local chown_err
    if ! chown_err="$(find -P "${DATA_DIR}" -xdev -depth -exec chown -h -- "${APP_USER}:${APP_USER}" {} + 2>&1)"; then
        log "WARNING: ownership repair of ${DATA_DIR} incomplete (content owned by another user needs DAC_OVERRIDE or a host-side chown -R 10001:10001): ${chown_err}"
    fi
}

fail_not_writable() {
    ls -ld -- "${DATA_DIR}" 2>/dev/null || true
    fail "${DATA_DIR} is not writable by the server user (uid 10001 as root-started, current uid $(id -u) otherwise); fix the volume owner on the host: chown -R 10001:10001"
}

# Validated before anything else runs, in both the root and the non-root
# branch - a bad DATA_DIR must not get as far as a chown or a started server.
validate_managed_dir "DATA_DIR" "${DATA_DIR}"

# Without arguments, or with a CLI mode/option (serve, validate, --env-file ...),
# the arguments go to the application; anything else is run as a plain command
# (e.g. `docker run <image> bash`) - but never before the privilege drop below.
if [[ "$#" -eq 0 ]]; then
    set -- python /app/run.py serve
elif [[ "$1" == "serve" || "$1" == "validate" || "$1" == -* ]]; then
    set -- python /app/run.py "$@"
fi

# Gated on the real UID alone, never on argv: caller-controlled arguments must
# not be able to skip the privilege drop.
if [[ "$(id -u)" -eq 0 ]]; then
    bootstrap_as_root
    log "Switching to user ${APP_USER} ..."
    # exec keeps the server as PID 1 so it receives SIGTERM directly.
    exec gosu "${APP_USER}" "$@"
fi

# Started with --user: no chown possible, only report an unusable volume.
[[ -d "${DATA_DIR}" && -w "${DATA_DIR}" ]] || fail_not_writable
exec "$@"
