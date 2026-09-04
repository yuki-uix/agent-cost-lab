#!/usr/bin/env bash
# Port guard for the recording proxy (issue #73). Sourced by scripts/capture.sh.
#
# A proxy that did not exit cleanly leaves 127.0.0.1:8787 held, so the next
# run's proxy dies with "[Errno 48] address already in use" while the old one
# keeps recording into the previous capture — the agent saw none of it. These
# two functions turn a half-dead proxy into a loud failure instead of silence:
#
#   acl_proxy_start <capture-file>   refuse to start on an occupied port, start
#                                    the proxy, then confirm the listener is the
#                                    PID we just started (not a stale one)
#   acl_proxy_stop                   kill the recorded PID, then wait until the
#                                    port is actually released before returning
#
# Both return non-zero and print the reason to stderr on failure, so a caller
# running under `set -e` aborts the whole capture rather than recording into
# the wrong file.

PORT="${PORT:-8787}"
PIDFILE="${PIDFILE:-.capture.pid}"
LOG="${LOG:-data/raw/proxy.log}"

_port_free() { ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; }

_listener_pid() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null; }

acl_proxy_start() {
    local capture_file="${1:?usage: acl_proxy_start <capture-file>}"

    # The port must be free before we start. Anything already listening here is
    # someone else's run; starting on top of it would stream into their file.
    if ! _port_free; then
        echo "port $PORT already in use (listener pid $(_listener_pid)); refusing to start a second proxy" >&2
        return 1
    fi

    ACL_CAPTURE="$capture_file" nohup "${ACL_PYTHON:-.venv/bin/python}" -m agentcostlab.proxy \
        >"$LOG" 2>&1 &
    local pid=$!
    echo "$pid" >"$PIDFILE"

    # The process listening on PORT must be the one we just started. If the
    # proxy crashed and something else took the port — or it never came up —
    # that is a failed start, not a recording.
    for _ in $(seq 40); do
        _listener_pid | grep -qx "$pid" && return 0
        sleep 0.25
    done
    echo "proxy pid $pid did not take port $PORT (listener pid $(_listener_pid)); see $LOG" >&2
    return 1
}

acl_proxy_stop() {
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"

    # `kill` returning is not enough: a proxy ignoring SIGTERM keeps the port,
    # and the next start would collide — the exact #73 failure mode.
    for _ in $(seq 40); do
        _port_free && return 0
        sleep 0.25
    done
    echo "port $PORT still in use after kill (listener pid $(_listener_pid)); aborting" >&2
    return 1
}
