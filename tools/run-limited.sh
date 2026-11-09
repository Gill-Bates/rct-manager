#!/usr/bin/env bash
#
# tools/run-limited.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# Purpose: Run one heavy local test command under a shared host-wide semaphore, a CPU budget and
#          worker caps, so parallel agent sessions cannot overload the machine.
# Scope:   Local use only. GitHub Actions does not use this wrapper and keeps its own settings.
#
# All sizes come from tools/test-budget.mjs (CPU affinity, cgroup limits, free RAM, load); nothing is
# hard-coded here. Overrides (all optional, validated there): RCT_TEST_SLOTS,
# RCT_TEST_CPU_SHARE, RCT_TEST_RUN_SHARE, RCT_TEST_CORES_PER_RUN, RCT_TEST_RAM_SHARE,
# RCT_TEST_RAM_PER_WORKER_MB, RCT_TEST_LOAD_THRESHOLD, RCT_TEST_LOAD_AWARE.
#
# Environment owned by this wrapper:
#   RCT_TEST_WAIT        seconds to wait for a free slot (default 3600; 0 = fail at once)
#   RCT_TEST_LOAD_WAIT   seconds to wait for a busy machine to calm down after a slot was
#                           acquired (default 60; the run then starts with the reduced budget)
#   RCT_TEST_CGROUP      auto (default) or off; auto uses systemd-run --scope CPUQuota when it works
#   RCT_TEST_LOCK_DIR    lock directory (default ${TMPDIR:-/tmp}/rct-test-slots-$UID),
#                           mode 0700, owned by the current user, never a symlink
#
# Without a verified CPU quota only one heavy run is allowed at a time, unless RCT_TEST_SLOTS is set.
#
# Exit codes: the command's own exit code; 124 = no slot within RCT_TEST_WAIT;
#             125 = wrapper setup error; 130/143 = interrupted by SIGINT/SIGTERM.

DEPENDENCIES=(flock setsid stat mkdir chmod sleep kill id node)

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WAIT_SECONDS="${RCT_TEST_WAIT:-3600}"
LOAD_WAIT_SECONDS="${RCT_TEST_LOAD_WAIT:-60}"
CGROUP_MODE="${RCT_TEST_CGROUP:-auto}"
STOP_GRACE_SECONDS=10
SLOTS=1 CPUS=1 PY_WORKERS=1 NODE_WORKERS=1 PW_WORKERS=1 BUSY=false
CGROUP_OK=false
CGROUP_UNIT=""
CGROUP_SYSTEMCTL=()

CMD=()
PREFIX=()
CGROUP_PREFIX=()
CHILD_PID=""
WATCHDOG_PID=""
RECEIVED_SIGNAL=""

function usage() {
    cat <<'EOF'
Usage: tools/run-limited.sh [--] COMMAND [ARGS...]

Runs COMMAND once a shared test slot is free, at nice 10 and, where systemd-run --scope works,
inside a cgroup with a CPUQuota. Slot count, cores per run and worker counts are derived from
this machine by tools/test-budget.mjs (run `node tools/test-budget.mjs` to see them). Arguments
are passed on unchanged, except that worker counts are capped to the budget (pytest -n,
node --test-concurrency, Playwright workers, RCT_E2E_WORKERS) and `npm test` becomes
`npm run test:local`. Without a verifiable CPU quota only one run at a time is allowed unless
RCT_TEST_SLOTS is set.

Dependencies: flock, setsid, stat, node (optional: nice, ionice, systemd-run)

Examples:
  tools/run-limited.sh pytest -n auto
  tools/run-limited.sh npx playwright test --config tests/e2e/playwright.config.mjs --project chromium
  RCT_TEST_SLOTS=1 tools/run-limited.sh npm test
EOF
    exit 1
}

function main() {
    if (( $# > 0 )) && [[ "$1" == "--" ]]; then
        shift
    fi
    case "${1:-}" in
    "" | -h | --help)
        usage
        ;;
    esac

    exit_on_missing_tools "${DEPENDENCIES[@]}"
    validate_settings

    if [[ -n "${RCT_RUN_LIMITED:-}" ]]; then
        run_nested "$@"
    fi

    load_budget
    detect_cgroup_scope
    prepare_lock_dir || exit 125
    acquire_slot
    wait_for_calm_machine
    finalize_environment "$@"
    build_prefix
    run_command
}

function exit_on_missing_tools() {
    local cmd
    for cmd in "$@"; do
        if command -v "$cmd" &>/dev/null; then
            continue
        fi
        printf "Error: Required tool '%s' is not installed or not in PATH\n" "$cmd" >&2
        exit 125
    done
}

function validate_settings() {
    local name value
    for name in RCT_TEST_WAIT RCT_TEST_LOAD_WAIT; do
        value="${!name:-0}"
        if [[ ! "$value" =~ ^[0-9]{1,7}$ ]]; then
            printf "Error: %s must be a non-negative integer, got '%s'\n" "$name" "$value" >&2
            exit 125
        fi
    done
    if [[ "$CGROUP_MODE" != "auto" && "$CGROUP_MODE" != "off" ]]; then
        printf "Error: RCT_TEST_CGROUP must be 'auto' or 'off', got '%s'\n" "$CGROUP_MODE" >&2
        exit 125
    fi
}

# Reads the computed budget; the key=value lines are parsed, never evaluated.
function load_budget() {
    local output key value
    if ! output="$(node "$SCRIPT_DIR/test-budget.mjs")"; then
        exit 125
    fi
    while IFS='=' read -r key value; do
        case "$key" in
        busy) BUSY="$value" ;;
        slots | runCores | pytestWorkers | nodeConcurrency | playwrightWorkers)
            [[ "$value" =~ ^[0-9]+$ ]] || continue
            case "$key" in
            slots) SLOTS="$value" ;;
            runCores) CPUS="$value" ;;
            pytestWorkers) PY_WORKERS="$value" ;;
            nodeConcurrency) NODE_WORKERS="$value" ;;
            playwrightWorkers) PW_WORKERS="$value" ;;
            esac
            ;;
        esac
    done <<<"$output"
}

# Nested call: the outer wrapper already holds the slot and the budget; only caps and env apply.
function run_nested() {
    local name
    # The inherited caps reach bash arithmetic, which would evaluate anything but a plain number.
    for name in RCT_LIMIT_PY RCT_LIMIT_NODE RCT_LIMIT_PW; do
        if [[ ! "${!name:-1}" =~ ^[1-9][0-9]{0,5}$ ]]; then
            printf "Error: %s must be a positive integer, got '%s'\n" "$name" "${!name}" >&2
            exit 125
        fi
    done
    PY_WORKERS="${RCT_LIMIT_PY:-1}" NODE_WORKERS="${RCT_LIMIT_NODE:-1}" PW_WORKERS="${RCT_LIMIT_PW:-1}"
    finalize_environment "$@"
    exec "${CMD[@]}"
}

function finalize_environment() {
    local layout="${RCT_E2E_WORKERS:-}"
    adjust_command "$@"
    if [[ -z "$layout" ]]; then
        layout="$PW_WORKERS"
    elif [[ "$layout" =~ ^[0-9]+$ ]] && (( layout > PW_WORKERS )); then
        printf "Note: RCT_E2E_WORKERS=%s lowered to %s\n" "$layout" "$PW_WORKERS" >&2
        layout="$PW_WORKERS"
    fi
    export RCT_RUN_LIMITED=1 RCT_LIMIT_PY="$PY_WORKERS" RCT_LIMIT_NODE="$NODE_WORKERS" RCT_LIMIT_PW="$PW_WORKERS"
    export PYTEST_XDIST_AUTO_NUM_WORKERS="$PY_WORKERS"
    export RCT_E2E_WORKERS="$layout"
}

function wait_for_calm_machine() {
    local waited=0
    while [[ "$BUSY" == "true" ]] && (( waited < LOAD_WAIT_SECONDS )) && [[ -z "$RECEIVED_SIGNAL" ]]; do
        if (( waited % 30 == 0 )); then
            printf "run-limited: machine is busy, waiting up to %s s before starting with %s core(s)\n" \
                "$LOAD_WAIT_SECONDS" "$CPUS" >&2
        fi
        sleep 1
        waited=$((waited + 1))
        if (( waited % 5 == 0 )); then
            load_budget
        fi
    done
    exit_if_signalled
    printf "run-limited: budget: %s core(s), pytest/node workers %s/%s, Playwright workers %s\n" \
        "$CPUS" "$PY_WORKERS" "$NODE_WORKERS" "$PW_WORKERS" >&2
}

function exit_if_signalled() {
    if [[ -n "$RECEIVED_SIGNAL" ]]; then
        printf "run-limited: interrupted before the command started\n" >&2
        exit "$([[ "$RECEIVED_SIGNAL" == INT ]] && echo 130 || echo 143)"
    fi
}

# Caps worker counts that would bypass the budget; everything else is copied unchanged.
function adjust_command() {
    local -a in=("$@")
    local is_pytest=false is_playwright=false is_node_test=false
    local i arg next
    for ((i = 0; i < ${#in[@]} && i < 4; i++)); do
        case "${in[i]##*/}" in
        pytest | py.test) is_pytest=true ;;
        playwright) is_playwright=true ;;
        esac
        if [[ "${in[i]}" == "-m" && "${in[i + 1]:-}" == "pytest" ]]; then
            is_pytest=true
        fi
    done
    for arg in "${in[@]}"; do
        if [[ "$arg" == */@playwright/test/cli.js ]]; then
            is_playwright=true
        fi
        if [[ "$arg" == --test ]]; then
            is_node_test=true
        fi
    done

    # `npm test [-- files]` would run node --test without a concurrency cap; the local script has one.
    if [[ "${in[0]##*/}" == "npm" && "${in[1]:-}" == "test" ]]; then
        CMD=(npm run test:local "${in[@]:2}")
        return 0
    fi
    if [[ "${in[0]##*/}" == "npm" && "${in[1]:-}" == "run" && "${in[2]:-}" == "test" ]]; then
        CMD=(npm run test:local "${in[@]:3}")
        return 0
    fi

    CMD=()
    for ((i = 0; i < ${#in[@]}; i++)); do
        arg="${in[i]}"
        next="${in[i + 1]:-}"
        if [[ "$is_pytest" == true ]]; then
            case "$arg" in
            -n | --numprocesses)
                CMD+=("$arg" "$(cap_workers "$next" "$PY_WORKERS")")
                ((i++))
                continue
                ;;
            -n?*)
                CMD+=("-n$(cap_workers "${arg#-n}" "$PY_WORKERS")")
                continue
                ;;
            --numprocesses=*)
                CMD+=("--numprocesses=$(cap_workers "${arg#*=}" "$PY_WORKERS")")
                continue
                ;;
            esac
        fi
        if [[ "$is_playwright" == true ]]; then
            case "$arg" in
            --workers | -j)
                CMD+=("$arg" "$(cap_workers "$next" "$PW_WORKERS")")
                ((i++))
                continue
                ;;
            --workers=*)
                CMD+=("--workers=$(cap_workers "${arg#*=}" "$PW_WORKERS")")
                continue
                ;;
            -j[0-9]*)
                CMD+=("-j$(cap_workers "${arg#-j}" "$PW_WORKERS")")
                continue
                ;;
            esac
        fi
        if [[ "${in[0]##*/}" == "node" ]]; then
            case "$arg" in
            --test-concurrency)
                CMD+=("--test-concurrency=$(cap_workers "$next" "$NODE_WORKERS")")
                ((i++))
                continue
                ;;
            --test-concurrency=*)
                CMD+=("--test-concurrency=$(cap_workers "${arg#*=}" "$NODE_WORKERS")")
                continue
                ;;
            --test)
                CMD+=("$arg")
                if [[ "$is_node_test" == true && "${in[*]}" != *"--test-concurrency"* ]]; then
                    CMD+=("--test-concurrency=$NODE_WORKERS")
                fi
                continue
                ;;
            esac
        fi
        CMD+=("$arg")
    done
}

# Numbers above the cap and everything that is not a plain number (auto, logical, 50%) become the cap.
function cap_workers() {
    local value="$1" cap="$2"
    if [[ "$value" == "0" ]]; then
        printf '0'
    elif [[ "$value" =~ ^[0-9]+$ ]] && (( value <= cap )); then
        printf '%s' "$value"
    else
        if [[ "$value" =~ ^[0-9]+$ ]]; then
            printf "Note: worker count %s lowered to %s\n" "$value" "$cap" >&2
        fi
        printf '%s' "$cap"
    fi
}

function prepare_lock_dir() {
    LOCK_DIR="${RCT_TEST_LOCK_DIR:-${TMPDIR:-/tmp}/rct-test-slots-$(id -u)}"
    if [[ -L "$LOCK_DIR" ]]; then
        printf "Error: lock directory '%s' is a symlink\n" "$LOCK_DIR" >&2
        return 1
    fi
    if [[ ! -d "$LOCK_DIR" ]]; then
        # shellcheck disable=SC2174 # only the deepest directory needs 0700, chmod below enforces it
        mkdir -p -m 0700 -- "$LOCK_DIR" 2>/dev/null || [[ -d "$LOCK_DIR" ]] || {
            printf "Error: cannot create lock directory '%s'\n" "$LOCK_DIR" >&2
            return 1
        }
    fi
    if [[ "$(stat -c %u -- "$LOCK_DIR")" != "$(id -u)" ]]; then
        printf "Error: lock directory '%s' is not owned by the current user\n" "$LOCK_DIR" >&2
        return 1
    fi
    chmod 0700 -- "$LOCK_DIR" || {
        printf "Error: cannot set mode 0700 on '%s'\n" "$LOCK_DIR" >&2
        return 1
    }
}

# Counts held slot locks and the smallest slot count any holder recorded (globals HELD, LIMIT).
function scan_slots() {
    local file base recorded
    HELD=0
    LIMIT="$SLOTS"
    for file in "$LOCK_DIR"/slot-*.lock; do
        [[ -f "$file" && ! -L "$file" ]] || continue
        if ( flock -n 7 ) 7<"$file" 2>/dev/null; then
            continue
        fi
        HELD=$((HELD + 1))
        base="${file%.lock}"
        recorded="$(cat -- "$base.n" 2>/dev/null)"
        if [[ "$recorded" =~ ^[0-9]+$ ]] && (( recorded >= 1 && recorded < LIMIT )); then
            LIMIT="$recorded"
        fi
    done
}

# Check and claim happen under a short mutex, so no two sessions pass the check together.
# The slot lock itself (fd 9) is held until this wrapper exits; the mutex (fd 8) is not.
function acquire_slot() {
    local waited=0 announced=-1 index file
    trap 'RECEIVED_SIGNAL=INT' INT
    trap 'RECEIVED_SIGNAL=TERM' TERM
    while true; do
        exec 8>>"$LOCK_DIR/mutex.lock" || {
            printf "Error: cannot open mutex file in '%s'\n" "$LOCK_DIR" >&2
            exit 125
        }
        if flock -w 2 8; then
            scan_slots
            if (( HELD < LIMIT )); then
                for ((index = 0; index < 1024; index++)); do
                    file="$LOCK_DIR/slot-$index.lock"
                    [[ -L "$file" ]] && continue
                    exec 9>>"$file" || continue
                    if flock -n 9; then
                        printf '%s\n' "$SLOTS" >"$LOCK_DIR/slot-$index.n"
                        exec 8>&-
                        trap - INT TERM
                        exit_if_signalled
                        printf "run-limited: acquired test slot %s of %s\n" "$((index + 1))" "$LIMIT" >&2
                        return 0
                    fi
                    exec 9>&-
                done
            fi
        fi
        exec 8>&-
        exit_if_signalled
        if (( waited >= WAIT_SECONDS )); then
            printf "Error: no free test slot within %s s (%s of %s busy, lock dir %s)\n" \
                "$WAIT_SECONDS" "$HELD" "$LIMIT" "$LOCK_DIR" >&2
            exit 124
        fi
        if (( waited / 30 != announced )); then
            announced=$((waited / 30))
            printf "run-limited: all %s test slots are busy, waiting (%s s of max %s s)...\n" \
                "$LIMIT" "$waited" "$WAIT_SECONDS" >&2
        fi
        sleep 1
        waited=$((waited + 1))
    done
}

function build_prefix() {
    PREFIX=()
    if command -v nice &>/dev/null; then
        PREFIX+=(nice -n 10)
    else
        printf "run-limited: nice not available, running at normal priority\n" >&2
    fi
    if command -v ionice &>/dev/null && ionice -c 2 -n 7 true &>/dev/null; then
        PREFIX+=(ionice -c 2 -n 7)
    fi
    if [[ "$CGROUP_OK" == true ]]; then
        CGROUP_UNIT="rct-test-$$-$RANDOM.scope"
        CGROUP_PREFIX=("${CGROUP_SCOPE[@]}" "--unit=$CGROUP_UNIT" -p "CPUQuota=$((CPUS * 100))%")
    fi
}

# Only claims a CPU limit after reading the quota back from a probe scope's cgroup. Runs before
# the slot is taken, because the verdict decides how many slots are safe.
function detect_cgroup_scope() {
    local expected observed quota
    CGROUP_OK=false
    if [[ "$CGROUP_MODE" == "off" ]]; then
        printf "run-limited: cgroup CPU limit disabled; only slots and worker caps are active\n" >&2
    elif ! command -v systemd-run &>/dev/null || [[ ! -d /run/systemd/system ]]; then
        printf "run-limited: systemd-run --scope unavailable; no CPU limit\n" >&2
    else
        CGROUP_SCOPE=(systemd-run --scope --quiet --collect)
        CGROUP_SYSTEMCTL=(systemctl)
        if [[ "$(id -u)" != "0" ]]; then
            CGROUP_SCOPE=(systemd-run --user --scope --quiet --collect)
            CGROUP_SYSTEMCTL=(systemctl --user)
        fi
        quota=$((CPUS * 100))
        expected="$((CPUS * 100000)) 100000"
        # shellcheck disable=SC2016 # expansion is meant to happen in the inner shell
        observed="$("${CGROUP_SCOPE[@]}" -p "CPUQuota=${quota}%" sh -c 'cat "/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)/cpu.max"' 2>/dev/null)"
        if [[ "$observed" == "$expected" ]]; then
            CGROUP_OK=true
            return 0
        fi
        printf "run-limited: CPUQuota could not be verified (read '%s'); no CPU limit\n" "${observed:-none}" >&2
    fi
    if [[ -z "${RCT_TEST_SLOTS:-}" ]]; then
        printf "run-limited: no verified CPU limit, allowing one heavy run at a time (set RCT_TEST_SLOTS to change)\n" >&2
        SLOTS=1
    fi
}

function stop_group() {
    kill "-$1" -- "-$CHILD_PID" 2>/dev/null
}

function group_alive() {
    kill -0 -- "-$CHILD_PID" 2>/dev/null
}

# SIGTERM is forwarded for both signals: background jobs of a non-interactive shell start with
# SIGINT ignored, so a forwarded SIGINT would not reach every command.
function on_signal() {
    RECEIVED_SIGNAL="$1"
    stop_group TERM
    if [[ -z "$WATCHDOG_PID" ]]; then
        ( sleep "$STOP_GRACE_SECONDS"; kill -KILL -- "-$CHILD_PID" 2>/dev/null ) 8>&- 9>&- &
        WATCHDOG_PID=$!
    fi
}

# The slot (fd 9) is released only when this wrapper exits, i.e. after this cleanup finished.
# A process that left the group with its own setsid is reached only through the verified scope.
function reap_tree() {
    local tick
    if group_alive; then
        stop_group TERM
        for ((tick = 0; tick < 30; tick++)); do
            group_alive || break
            sleep 0.1
        done
    fi
    if group_alive; then
        stop_group KILL
        for ((tick = 0; tick < 30; tick++)); do
            group_alive || break
            sleep 0.1
        done
    fi
    if [[ -n "$CGROUP_UNIT" ]]; then
        "${CGROUP_SYSTEMCTL[@]}" kill --signal=SIGKILL "$CGROUP_UNIT" &>/dev/null
    fi
}

function run_command() {
    local status
    trap 'on_signal TERM' TERM
    trap 'on_signal INT' INT
    # Own session = own process group, so cleanup reaches browsers and stub apps but nothing else.
    # fds 8/9 are closed for the child: a leaked daemon must not keep the slot after the wrapper ends.
    setsid "${CGROUP_PREFIX[@]}" "${PREFIX[@]}" "${CMD[@]}" <&0 8>&- 9>&- &
    CHILD_PID=$!
    wait "$CHILD_PID"
    status=$?
    while kill -0 "$CHILD_PID" 2>/dev/null; do
        wait "$CHILD_PID"
        status=$?
    done
    reap_tree
    trap - INT TERM
    if [[ -n "$WATCHDOG_PID" ]]; then
        kill "$WATCHDOG_PID" 2>/dev/null
    fi
    if [[ -n "$RECEIVED_SIGNAL" && "$status" -ge 128 ]]; then
        status="$([[ "$RECEIVED_SIGNAL" == INT ]] && echo 130 || echo 143)"
    fi
    exit "$status"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
    # shellcheck disable=SC2317 # main always exits; kept for the house guard-clause pattern
    exit 0
fi
