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
#   acl_proxy_stop                   confirm the recorded PID is the listener,
#                                    then kill it and wait until the port is
#                                    actually released before returning
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

    # Failed start: nothing we wrote has been verified, so leave none of it
    # behind. The child we just launched must not survive to be mistaken for a
    # listener by the next start/stop, and the pidfile is exactly the stale
    # value that stop would otherwise feed on.
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.05
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "proxy pid $pid did not take port $PORT (listener pid $(_listener_pid)); see $LOG" >&2
    return 1
}

acl_proxy_stop() {
    local pid listener
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    listener=$(_listener_pid)

    # The port is the truth; the pidfile is only a cache of who we last saw own
    # it. Acting on the stored value alone kills whichever process happens to
    # have recycled that pid — the exact half-dead-proxy failure this guard
    # exists to stop. Identity is taken from the port, never from the pidfile.
    if [ -z "$pid" ]; then
        if _port_free; then
            rm -f "$PIDFILE"
            return 0
        fi
        echo "no pidfile $PIDFILE, but port $PORT is held by pid $listener; refusing to kill an unrecorded process" >&2
        return 1
    fi

    if [ "$listener" != "$pid" ]; then
        rm -f "$PIDFILE"
        if _port_free; then
            echo "pidfile $PIDFILE named pid $pid, but port $PORT is free; not killing (proxy already gone)" >&2
        else
            echo "pidfile $PIDFILE named pid $pid, but port $PORT is held by pid $listener; not killing" >&2
        fi
        return 1
    fi

    kill "$pid" 2>/dev/null || true
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
