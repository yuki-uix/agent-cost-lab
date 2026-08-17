"""Recording proxy — the only instrument in this repo.

It sits on the real call path: it does not reconstruct requests, re-implement
provider logic, or simulate anything. Whatever the agent actually sent is what
gets recorded.

    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude

Streaming behaviour (incremental delivery, mid-stream parse, clean cancellation
on client disconnect) is covered by tests/test_proxy_sse.py.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

UPSTREAM = os.environ.get("ACL_UPSTREAM", "https://api.anthropic.com")
PROVIDER = os.environ.get("ACL_PROVIDER", "anthropic")
CAPTURE = Path(os.environ.get("ACL_CAPTURE", "data/raw/capture.jsonl"))
BETA = "cache-diagnosis-2026-04-07"

# Hop-by-hop headers, or ones httpx recomputes. Everything else (including the
# caller's own auth) is forwarded untouched.
STRIP = {"host", "content-length", "accept-encoding", "connection"}

LAST_ID: dict[str, str | None] = {"id": None}


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # One long-lived client. Closing it per-request truncates streams — that is
    # the classic bug this structure avoids.
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    yield
    await app.state.client.aclose()


app = FastAPI(lifespan=lifespan)


def _inject_diagnostics(body: dict, headers: dict) -> None:
    """Opt in to Anthropic cache diagnostics on EVERY turn.

    Turning this on mid-conversation would itself change the beta-header set and
    cost one self-inflicted miss, so it is all-or-nothing per session.
    """
    body["diagnostics"] = {"previous_message_id": LAST_ID["id"]}
    betas = [b for b in headers.get("anthropic-beta", "").split(",") if b.strip()]
    if BETA not in betas:
        betas.append(BETA)
    headers["anthropic-beta"] = ",".join(betas)


@app.post("/v1/messages")
async def messages(request: Request):
    raw = await request.body()
    body = json.loads(raw)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in STRIP}

    if PROVIDER == "anthropic":
        _inject_diagnostics(body, headers)

    payload = json.dumps(body).encode()
    t0 = time.perf_counter()
    record: dict = {
        "t_start": time.time(),
        "provider": PROVIDER,
        "model": body.get("model"),
        "request_bytes": len(payload),
        "injected_previous_message_id": LAST_ID["id"],
        "request_body": body,
        "system_prompt": body.get("system"),
        "tool_names": [t.get("name") for t in body.get("tools", [])],
        "usage": None,
        "diagnostics": None,
        "response_id": None,
        "first_byte_ms": None,
    }

    client: httpx.AsyncClient = request.app.state.client
    req = client.build_request(
        "POST", f"{UPSTREAM}/v1/messages", content=payload, headers=headers
    )

    async def gen():
        buf, seen_start = b"", False
        try:
            resp = await client.send(req, stream=True)
            try:
                async for chunk in resp.aiter_raw():
                    if record["first_byte_ms"] is None:
                        record["first_byte_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                    # Pass through first, parse second: the copy never delays the client.
                    yield chunk
                    if not seen_start:
                        buf += chunk
                        if b"message_start" in buf and b"\n\n" in buf:
                            seen_start = _parse_start(buf, record)
                            if seen_start:
                                buf = b""
            finally:
                await resp.aclose()
        finally:
            # Written even when the client aborts mid-stream, so partial
            # sessions still show up in the ledger instead of vanishing.
            record["t_end"] = time.time()
            CAPTURE.parent.mkdir(parents=True, exist_ok=True)
            with CAPTURE.open("a") as fh:
                fh.write(json.dumps(record) + "\n")

    return StreamingResponse(gen(), media_type="text/event-stream")


def _parse_start(buf: bytes, record: dict) -> bool:
    for block in buf.split(b"\n\n"):
        for line in block.split(b"\n"):
            if not line.startswith(b"data: "):
                continue
            try:
                payload = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if payload.get("type") != "message_start":
                continue
            msg = payload["message"]
            record["usage"] = msg.get("usage")
            record["diagnostics"] = msg.get("diagnostics")
            record["response_id"] = msg.get("id")
            LAST_ID["id"] = msg.get("id")
            return True
    return False
