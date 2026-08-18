"""Streaming mechanics of the proxy — the one real technical risk of the stack.

A buffering proxy would still pass a naive "did I get the bytes" test, so these
assert on *arrival timing* and on cancellation propagating upstream.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
PROXY, UPSTREAM = "http://127.0.0.1:8788", "http://127.0.0.1:8801"
BODY = {"model": "claude-opus-5", "max_tokens": 64, "system": "You are a test.",
        "tools": [{"name": "read_file"}],
        "messages": [{"role": "user", "content": "hi"}], "stream": True}


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    capture = tmp_path_factory.mktemp("capture") / "capture.jsonl"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
           "ACL_UPSTREAM": UPSTREAM, "ACL_PROVIDER": "anthropic",
           "ACL_CAPTURE": str(capture)}
    procs = [
        subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_upstream.py")],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([sys.executable, "-m", "uvicorn", "agentcostlab.proxy:app",
                          "--host", "127.0.0.1", "--port", "8788", "--log-level", "warning"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    for _ in range(100):
        try:
            httpx.get(f"{UPSTREAM}/_last", timeout=0.5)
            httpx.post(f"{PROXY}/v1/messages", json=BODY, timeout=5.0)
            break
        except Exception:
            time.sleep(0.1)
    yield capture
    for p in procs:
        p.terminate()
        p.wait(timeout=5)


def _stream(stop_after=None):
    async def run():
        arrivals = []
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=30.0) as c:
            async with c.stream("POST", f"{PROXY}/v1/messages", json=BODY,
                                headers={"anthropic-beta": "some-other-beta"}) as r:
                async for line in r.aiter_lines():
                    if line.startswith("event: "):
                        arrivals.append((line[7:], (time.perf_counter() - t0) * 1000))
                        if stop_after and len(arrivals) >= stop_after:
                            break
        return arrivals

    return asyncio.run(run())


def _records(capture: Path) -> list[dict]:
    return [json.loads(l) for l in capture.read_text().splitlines()]


def test_delivery_is_incremental_not_buffered(servers):
    arrivals = _stream()
    assert [a[0] for a in arrivals][:1] == ["message_start"]
    assert len(arrivals) == 7
    gaps = [arrivals[i][1] - arrivals[i - 1][1] for i in range(1, len(arrivals))]
    # Upstream emits every 150ms. A buffering proxy collapses these to ~0.
    assert all(g > 80 for g in gaps), f"looks buffered: {gaps}"
    assert arrivals[0][1] < 100, "message_start should not wait for the body"


def test_diagnostics_injected_without_clobbering_existing_betas(servers):
    _stream()
    seen = httpx.get(f"{UPSTREAM}/_last", timeout=5).json()
    assert "cache-diagnosis-2026-04-07" in seen["last_headers"]["anthropic-beta"]
    assert "some-other-beta" in seen["last_headers"]["anthropic-beta"]
    assert "diagnostics" in seen["last_body"]


def test_previous_message_id_threads_across_turns(servers):
    _stream()
    _stream()
    seen = httpx.get(f"{UPSTREAM}/_last", timeout=5).json()
    assert seen["last_body"]["diagnostics"]["previous_message_id"] == "msg_fake_01"
    rec = _records(servers)[-1]
    assert rec["diagnostics"]["cache_miss_reason"]["type"] == "system_changed"


def test_client_disconnect_propagates_and_still_records(servers):
    before = len(_records(servers))
    t0 = time.perf_counter()
    _stream(stop_after=2)
    elapsed = (time.perf_counter() - t0) * 1000
    time.sleep(0.6)
    seen = httpx.get(f"{UPSTREAM}/_last", timeout=5).json()
    assert elapsed < 700, "proxy hung instead of releasing the client"
    assert seen["cancelled"] is True, "upstream kept generating after client left"
    assert len(_records(servers)) == before + 1, "aborted stream vanished from ledger"


def test_serialiser_is_byte_faithful_on_non_ascii():
    """Regression: default json.dumps escaped CJK and grew a payload by 39%."""
    from agentcostlab.proxy import serialise

    original = ('{"model":"claude-opus-5","system":"你是一个测试助手",'
                '"messages":[{"role":"user","content":"帮我修一下 billing 里的 bug"}]}')
    out = serialise(json.loads(original))
    assert out == original.encode(), "proxy rewrote the payload it is measuring"
    assert b"\\u" not in out


def test_non_ascii_reaches_upstream_intact(servers):
    cjk = {**BODY, "system": "你是一个测试助手",
           "messages": [{"role": "user", "content": "帮我修一下 billing 里的 bug"}]}

    async def run():
        async with httpx.AsyncClient(timeout=30.0) as c:
            async with c.stream("POST", f"{PROXY}/v1/messages", json=cjk) as r:
                async for _ in r.aiter_lines():
                    pass

    asyncio.run(run())
    seen = httpx.get(f"{UPSTREAM}/_last", timeout=5).json()
    assert seen["last_body"]["system"] == "你是一个测试助手"
    rec = _records(servers)[-1]
    # Only the injected diagnostics field should account for the growth.
    overhead = rec["request_bytes"] - rec["client_bytes"]
    assert overhead < 80, f"unexplained growth of {overhead} bytes"


def test_captured_record_passes_the_export_gate(servers):
    """Two-way check: a new capture field with no redact Policy fails here."""
    from agentcostlab.redact import redact

    _stream()
    out = redact(_records(servers)[-1])
    assert out["usage"]["cache_read_input_tokens"] == 1200
    # HASH policy keeps the key but not the content.
    assert out["request_body"].startswith("sha256:")
    assert out["system_prompt"].startswith("sha256:")
    assert "You are a test." not in str(out)


def test_upstream_error_status_is_not_laundered_into_200(servers):
    """Regression: the proxy used to hardcode 200, so a failed upstream call
    reached the client as 'HTTP 200 with a malformed body'."""
    r = httpx.post(f"{PROXY}/v1/messages", json={**BODY, "model": "error-400"}, timeout=30)
    assert r.status_code == 400, f"upstream 400 surfaced as {r.status_code}"
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_unreachable_upstream_returns_502_not_an_empty_200(servers):
    """A connect failure must not be reported as success with no body."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
           "ACL_UPSTREAM": "http://127.0.0.1:9", "ACL_PROVIDER": "anthropic",
           "ACL_CAPTURE": str(servers)}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "agentcostlab.proxy:app",
                          "--host", "127.0.0.1", "--port", "8789", "--log-level", "error"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                r = httpx.post("http://127.0.0.1:8789/v1/messages", json=BODY, timeout=20)
                break
            except Exception:
                time.sleep(0.1)
        else:
            pytest.fail("proxy never came up")
        assert r.status_code == 502, f"unreachable upstream surfaced as {r.status_code}"
        assert r.json()["error"]["type"] == "proxy_upstream_error"
    finally:
        p.terminate(); p.wait(timeout=5)


def test_non_streaming_reply_keeps_its_content_type_and_records_usage(servers):
    """Non-SSE replies were previously re-labelled text/event-stream."""
    r = httpx.post(f"{PROXY}/v1/messages", json={**BODY, "stream": False}, timeout=30)
    assert r.status_code == 200
    assert "application/json" in r.headers["content-type"]
    assert r.json()["id"] == "msg_fake_json"
    rec = _records(servers)[-1]
    assert rec["usage"]["input_tokens"] == 7, "usage lost on non-streamed calls"
    assert rec["status_code"] == 200


def test_gzipped_upstream_still_yields_usage(servers):
    """Regression: with a compressed stream, aiter_raw() gave undecoded bytes,
    so message_start never matched and every record lost its usage — while the
    client saw a perfectly good response. Silent instrument failure."""
    before = len(_records(servers))
    r = httpx.post(f"{PROXY}/v1/messages", json={**BODY, "model": "gzip-me"},
                   headers={"accept-encoding": "gzip"}, timeout=30)
    assert r.status_code == 200
    recs = _records(servers)
    assert len(recs) == before + 1
    assert recs[-1]["usage"] is not None, "usage lost when upstream compresses"


def test_uncooperative_upstream_is_recorded_not_silently_dropped(servers):
    """identity is a request, not a guarantee.

    If upstream compresses anyway the client still decodes fine, so nothing
    looks wrong — but the parse fails and usage vanishes. That must leave a
    trace in the ledger rather than a plausible-looking zero.
    """
    r = httpx.post(f"{PROXY}/v1/messages", json={**BODY, "model": "gzip-always"}, timeout=30)
    assert r.status_code == 200, "the client's view must be unaffected"
    rec = _records(servers)[-1]
    assert rec["error"] and "ignored identity" in rec["error"], (
        f"silent ledger loss: usage={rec['usage']} error={rec['error']}"
    )


def test_observer_never_breaks_the_stream_it_observes(servers):
    """A parse failure must not propagate out of the response generator.

    JSONDecodeError was caught but UnicodeDecodeError was not, so a gzipped
    body raised mid-stream and the client got a truncated response.
    """
    r = httpx.post(f"{PROXY}/v1/messages", json={**BODY, "model": "gzip-always"}, timeout=30)
    assert r.status_code == 200
    assert len(r.content) > 0, "stream truncated by the proxy's own parser"


@pytest.mark.parametrize("buf,label", [
    (b'data: \x8b\xff\n\n', "non-UTF8 bytes"),
    (b'data: 1\n\n', "JSON scalar instead of an object"),
    (b'data: {"type":"message_start"}\n\n', "message_start with no message key"),
    (b'data: {"type":"message_start","message":"nope"}\n\n', "message is not an object"),
])
def test_parse_start_never_raises_on_malformed_sse(buf, label):
    """_parse_start runs inside the streaming loop, so anything it raises
    truncates the client's response. These assert no exception, not success."""
    from agentcostlab.proxy import _parse_start

    _parse_start(buf, {})  # must not raise


def test_a_broken_parser_cannot_damage_the_stream():
    """Structural guard: even a parser that always raises stays contained."""
    from agentcostlab.proxy import _observe

    def exploding(buf, record):
        raise RuntimeError("boom")

    rec: dict = {}
    assert _observe(exploding, b"x", rec) is None
    assert "boom" in rec["error"]
