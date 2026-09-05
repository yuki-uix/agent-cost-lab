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
# Both print the reason to stderr when they cannot do what was asked. Their
# exit codes are a contract for the caller (scripts/capture.sh, and E4's arm
# runner which sources this file from outside the repo and can only read this
# comment):
#
#   acl_proxy_stop returns
#     0  the proxy was stopped, or nothing was running and no pidfile remained
#     2  the port is free and only the pidfile remains — the proxy is already
#        gone. This is a warning, not a stop that could not be performed: no
#        process is writing the capture any more, so the caller must NOT abort
#        and should still run the health gate.
#     1  the port is held by a process that is not the recorded PID, or the
#        port could not be released after the kill. Acting would kill a process
#        that was never verified, so the caller MUST abort the capture without
#        running the health gate (the capture may still be growing).
#
#   acl_proxy_start returns 0 on success and 1 on any failure. It has no
#   warning tier: a non-zero always means abort, so no per-code contract is
#   needed there.

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
        if _port_free; then
            # Proxy already gone: the port is free and only the pidfile is left.
            # Clean the residue and report it as a warning (exit 2), not a stop
            # that could not be performed — the caller should still run the
            # health gate, because nothing is writing the capture any more.
            rm -f "$PIDFILE"
            echo "pidfile $PIDFILE named pid $pid, but port $PORT is free; not killing (proxy already gone)" >&2
            return 2
        fi
        # The port is held by a process that is not the one we recorded. Acting
        # would kill a process we never verified, so abort (exit 1). The pidfile
        # is kept on purpose: it names the owner we last recorded, which is the
        # one piece of state that explains the collision to whoever debugs it.
        echo "pidfile $PIDFILE named pid $pid, but port $PORT is held by pid $listener; not killing" >&2
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
