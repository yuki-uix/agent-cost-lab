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

PORT="${PORT:-8787}"
PIDFILE="${PIDFILE:-.capture.pid}"
LOG="${LOG:-data/raw/proxy.log}"

# Port guard (issue #73): start/stop must refuse a half-dead proxy rather than
# silently record into the wrong capture. See scripts/proxy_guard.sh.
source "$(dirname "$0")/proxy_guard.sh"

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
    CAPTURE=$(next_file)
    mkdir -p data/raw
    acl_proxy_start "$CAPTURE" || exit 1
    echo "$CAPTURE" >"$PIDFILE.file"
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
    if _port_free; then
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
    acl_proxy_stop
    rc=$?
    # Exit 1 means the guard could not act safely (a foreign process holds the
    # port): the capture may still be growing, so the health gate would judge a
    # moving file. Abort without it. Exit 2 is only a warning — the proxy is
    # already gone and nothing is writing the capture, so the health gate's
    # verdict is trustworthy; run it and let its exit code be ours.
    [ "$rc" -eq 1 ] && exit 1
    rm -f "$PIDFILE.file"
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
