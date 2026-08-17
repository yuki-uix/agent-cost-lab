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
