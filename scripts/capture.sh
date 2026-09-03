#!/usr/bin/env bash
# Start (or check, or stop) the recording proxy for a capture session.
#
#   scripts/capture.sh          start recording into the next free capture file
#   scripts/capture.sh status   is it running, and how much has it recorded
#   scripts/capture.sh stop     stop it and run the health gate
#
# The proxy is detached with nohup on purpose. The 2026-08-19 session ended
# early because the proxy died with the terminal it was started in, and the
# agent session went with it.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PORT=8787
PIDFILE=.capture.pid
LOG=data/raw/proxy.log

listening() { lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; }

# The capture file does not exist until the first request lands, so a bare
# `wc -l < "$f"` fails on its redirect before `|| echo 0` can help.
count() { [ -f "$1" ] && wc -l <"$1" | tr -d ' ' || echo 0; }

# Never overwrite a capture. Three have already been discarded; the surviving
# ones are the only real data this project has.
next_file() {
    local n=3
    while [ -e "data/raw/capture-$(printf %02d $n).jsonl" ]; do n=$((n + 1)); done
    echo "data/raw/capture-$(printf %02d $n).jsonl"
}

case "${1:-start}" in
start)
    if listening; then
        echo "already recording into $(cat $PIDFILE.file 2>/dev/null || echo '?')"
        echo "run 'scripts/capture.sh status' to see it, or 'stop' to finish."
        exit 0
    fi
    CAPTURE=$(next_file)
    mkdir -p data/raw
    ACL_CAPTURE="$CAPTURE" nohup "${ACL_PYTHON:-.venv/bin/python}" -m agentcostlab.proxy \
        >"$LOG" 2>&1 &
    echo $! >"$PIDFILE"
    echo "$CAPTURE" >"$PIDFILE.file"
    for _ in $(seq 40); do listening && break; sleep 0.25; done
    if ! listening; then
        echo "proxy did not come up; see $LOG" >&2
        exit 1
    fi
    echo "recording into $CAPTURE   (pid $(cat $PIDFILE), log $LOG)"
    echo
    echo "Now, in the directory you actually want to work in:"
    echo
    echo "    ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT claude"
    echo
    echo "Work normally. Somewhere in the session, do these three — each one"
    echo "breaks the cache for a different reason the API can name:"
    echo "    1. switch model mid-conversation, then keep talking in it"
    echo "    2. /compact, then keep talking"
    echo "    3. add or remove an MCP server, then keep talking"
    echo
    echo "When you are done:  scripts/capture.sh stop"
    ;;
status)
    CAPTURE=$(cat $PIDFILE.file 2>/dev/null)
    if ! listening; then
        echo "not recording."
        [ -n "$CAPTURE" ] && echo "last capture: $CAPTURE ($(count "$CAPTURE") records)"
        exit 0
    fi
    echo "recording into $CAPTURE — $(count "$CAPTURE") records so far"
    "${ACL_PYTHON:-.venv/bin/python}" - "$CAPTURE" <<'PY'
import json, sys, collections
try:
    rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
except FileNotFoundError:
    sys.exit()
verdicts = collections.Counter()
for r in rows:
    d = r.get("diagnostics")
    if isinstance(d, dict) and isinstance(d.get("cache_miss_reason"), dict):
        verdicts[d["cache_miss_reason"]["type"]] += 1
useful = {k: v for k, v in verdicts.items()
          if k not in ("unavailable", "previous_message_not_found")}
print(f"  official verdicts so far: {sum(useful.values())} usable"
      + (f"  {dict(useful)}" if useful else "  <- still 0; the three actions are what produce these")
      + (f"   ({sum(verdicts.values()) - sum(useful.values())} inconclusive)"
         if len(verdicts) > len(useful) else ""))
PY
    ;;
stop)
    CAPTURE=$(cat $PIDFILE.file 2>/dev/null)
    [ -f "$PIDFILE" ] && kill "$(cat $PIDFILE)" 2>/dev/null
    rm -f "$PIDFILE" "$PIDFILE.file"
    echo "stopped."
    [ -z "$CAPTURE" ] && exit 0
    echo
    "${ACL_PYTHON:-.venv/bin/python}" scripts/capture_health.py "$CAPTURE"
    ;;
*)
    echo "usage: scripts/capture.sh [start|status|stop]" >&2
    exit 1
    ;;
esac
