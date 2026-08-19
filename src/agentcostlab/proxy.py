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
import hashlib
import json
import os
import time
from pathlib import Path

import httpx

from .codec import serialise
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

UPSTREAM = os.environ.get("ACL_UPSTREAM", "https://api.anthropic.com")
PROVIDER = os.environ.get("ACL_PROVIDER", "anthropic")
CAPTURE = Path(os.environ.get("ACL_CAPTURE", "data/raw/capture.jsonl"))
BETA = "cache-diagnosis-2026-04-07"

# Hop-by-hop headers, or ones httpx recomputes. Everything else (including the
# caller's own auth) is forwarded untouched.
STRIP = {"host", "content-length", "accept-encoding", "connection"}

# One slot per conversation lineage, keyed by the fields that stay fixed for a
# conversation's whole life: the model and the first message.
LAST_ID: dict[str, str | None] = {}


def _lineage_key(body: dict) -> str:
    """Stable identity for the conversation lineage a request belongs to.

    Keyed on ``messages[0]`` alone: the first message is the turn every later
    turn grows out of.

    ``model`` used to be hashed in too, on the reasoning that it is fixed for a
    conversation's life. It is not. Switching model mid-conversation is one of
    the three actions the capture plan asks for, and the only one that can
    produce ``model_changed`` — and hashing the model in turned that switch into
    a brand-new lineage, whose next request sends ``previous_message_id: null``
    and gets no diagnostics. Measured on capture-03: record 28 shares record
    27's ``messages[0]`` and differs only in model, and the proxy sent null at
    exactly the request where Anthropic would have named the reason.

    That is the failure the paragraph below already described for ``system`` and
    ``tools``. ``model`` belongs with them, and the reason it does not belong in
    the key is the same reason they do not: **it is one of the things E1
    measures changing.** Anything that changes during the events this instrument
    exists to record must stay out of the identity of what it is recording.

    So: the system prompt and the tool list both change mid-conversation — that
    is exactly the cache killer E1 sets out to measure — and hashing either of
    them in would erase it the same way.

    Not fixed by this: ``messages[0]`` itself changes across a ``/compact``,
    which starts a conversation with a new first message. That erases
    ``messages_changed`` the same way, and threading across it needs a design
    decision rather than a smaller key — see the discussion on #41.

    sha256 over a canonical JSON encoding, not hash(): hash() is salted by
    PYTHONHASHSEED (unstable across processes) and cannot hash a dict.

    Defensive about shape: this runs on the request path, before any response
    exists, so it is not covered by the _observe guard. A body is only known to
    be valid JSON, not valid for the API — ``{"messages": {...}}`` parses fine
    and used to raise KeyError here, turning upstream's 400 into a proxy 500 and
    hiding the real error from the caller.

    The collision this opens — two conversations whose first messages are
    byte-identical now share a lineage — was measured across all three captures
    before the change: 0 merges in the first two, and exactly 1 in the third,
    which is the model switch this fixes. Counted a second way, by looking for a
    lineage containing several disjoint chains, all 57 lineage groups across the
    three captures are clean.

    **The cross-lineage health gate cannot guard this.** It asks whether a
    request's predecessor belongs to a different lineage, and it asks that with
    this very function — so two conversations that collide here are, to it, one
    conversation, and the check is vacuously true. A collision would produce
    #14's failure mode (a plausible-looking wrong verdict, not an error) with
    the gate built to catch #14 looking straight past it.

    The independent detector is "one lineage, several disjoint chains", which
    does not consult this function. It is not wired into the health gate yet.
    """
    messages = body.get("messages")
    first = messages[0] if isinstance(messages, list) and messages else None
    raw = json.dumps(first, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # One long-lived client. Closing it per-request truncates streams — that is
    # the classic bug this structure avoids.
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    yield
    await app.state.client.aclose()


app = FastAPI(lifespan=lifespan)


def _inject_diagnostics(body: dict, headers: dict, key: str) -> None:
    """Opt in to Anthropic cache diagnostics on EVERY turn.

    Turning this on mid-conversation would itself change the beta-header set and
    cost one self-inflicted miss, so it is all-or-nothing per session.
    """
    body["diagnostics"] = {"previous_message_id": LAST_ID.get(key)}
    betas = [b for b in headers.get("anthropic-beta", "").split(",") if b.strip()]
    if BETA not in betas:
        betas.append(BETA)
    headers["anthropic-beta"] = ",".join(betas)


def _write(record: dict) -> None:
    record.setdefault("t_end", time.time())
    CAPTURE.parent.mkdir(parents=True, exist_ok=True)
    with CAPTURE.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


# Hop-by-hop headers we must not echo back: we re-stream the raw body, so the
# framework recomputes framing itself.
DROP_RESPONSE = {"content-length", "transfer-encoding", "connection"}


@app.post("/v1/messages")
async def messages(request: Request):
    raw = await request.body()
    body = json.loads(raw)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in STRIP}

    key = _lineage_key(body)
    if PROVIDER == "anthropic":
        _inject_diagnostics(body, headers, key)

    # Ask upstream not to compress. aiter_raw() yields undecoded bytes, so a
    # gzipped stream passes through fine but is unparseable here — the ledger
    # silently loses usage on every call while the client sees nothing wrong.
    # Locally the bandwidth is irrelevant; a readable stream is not.
    headers["accept-encoding"] = "identity"

    payload = serialise(body)
    t0 = time.perf_counter()
    record: dict = {
        "t_start": time.time(),
        "provider": PROVIDER,
        "model": body.get("model"),
        "client_bytes": len(raw),
        "request_bytes": len(payload),
        "injected_previous_message_id": LAST_ID.get(key),
        "request_body": body,
        "system_prompt": body.get("system"),
        "tool_names": [t.get("name") for t in body.get("tools", [])],
        "usage": None,
        "diagnostics": None,
        # Whether upstream's reply carried a `diagnostics` key at all, as
        # opposed to carrying it with a null value. Those two mean opposite
        # things — "the beta never took effect" versus "compared, cache hit" —
        # and `msg.get("diagnostics")` collapses them both to None. Without
        # this the health gate cannot tell a clean session from a dead
        # instrument, which is exactly what it exists to do.
        "diagnostics_present": None,
        "response_id": None,
        "first_byte_ms": None,
        "status_code": None,
        "error": None,
        # False until the response body is fully read. A client that cancels
        # mid-stream leaves a record with no usage and nothing wrong — without
        # this flag that is indistinguishable from a parse failure.
        "stream_complete": False,
    }

    client: httpx.AsyncClient = request.app.state.client
    req = client.build_request(
        "POST", f"{UPSTREAM}/v1/messages", content=payload, headers=headers
    )

    # Send before returning: the status and content-type have to be known up
    # front. Starting a StreamingResponse first commits us to 200, so an
    # upstream failure would reach the client as "HTTP 200, empty body" —
    # which is exactly what it did before this was fixed.
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        _write(record)
        return JSONResponse(
            status_code=502,
            content={"type": "error", "error": {"type": "proxy_upstream_error",
                                                "message": record["error"]}},
        )

    record["status_code"] = resp.status_code
    ctype = resp.headers.get("content-type", "application/json")

    # identity is a request, not a guarantee. If upstream compresses anyway,
    # aiter_raw() yields bytes we cannot parse and the ledger would quietly
    # zero out while the client decodes the stream and notices nothing. Record
    # it: a logged failure is recoverable, a silent one corrupts the dataset.
    enc = resp.headers.get("content-encoding", "identity").strip().lower()
    if enc and enc != "identity":
        record["error"] = f"upstream ignored identity encoding: {enc}"
    passthrough = {k: v for k, v in resp.headers.items()
                   if k.lower() not in DROP_RESPONSE and k.lower() != "content-type"}

    async def gen():
        buf, parsed = b"", False
        try:
            async for chunk in resp.aiter_raw():
                if record["first_byte_ms"] is None:
                    record["first_byte_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                # Pass through first, parse second: the copy never delays the client.
                yield chunk
                if not parsed:
                    buf += chunk
                    if b"message_start" in buf and b"\n\n" in buf:
                        parsed = _observe(_parse_start, buf, record) or False
                        if parsed:
                            buf = b""
            if not parsed and buf and enc == "identity":
                _observe(_parse_json_body, buf, record)
            record["stream_complete"] = True
        finally:
            await resp.aclose()
            # Written even when the client aborts mid-stream, so partial
            # sessions still show up in the ledger instead of vanishing.
            _write(record)

    return StreamingResponse(gen(), status_code=resp.status_code,
                             media_type=ctype, headers=passthrough)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def passthrough(path: str, request: Request):
    """Anything that is not /v1/messages still has to work.

    Claude Code calls other endpoints (token counting, and more). Without this
    they 404 at the proxy and the client reports a malformed response.
    """
    raw = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in STRIP}
    client: httpx.AsyncClient = request.app.state.client
    try:
        resp = await client.request(request.method, f"{UPSTREAM}/{path}",
                                    content=raw, headers=headers,
                                    params=dict(request.query_params))
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content={
            "type": "error",
            "error": {"type": "proxy_upstream_error",
                      "message": f"{type(exc).__name__}: {exc}"}})
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type"))


def _observe(fn, buf: bytes, record: dict):
    """Run a parser so that it can never damage the stream it is reading.

    Hardening each parser individually has now failed twice in this file: the
    fix went to the path that triggered, and the sibling kept the same bug. The
    guard is structural so a future parser inherits it by default.
    """
    try:
        return fn(buf, record)
    except Exception as exc:  # noqa: BLE001 - observing must never be fatal
        record["error"] = f"parse failed: {type(exc).__name__}: {exc}"
        return None


def _parse_start(buf: bytes, record: dict) -> bool:
    """Pull usage/diagnostics out of the SSE message_start event."""
    for block in buf.split(b"\n\n"):
        for line in block.split(b"\n"):
            if not line.startswith(b"data: "):
                continue
            try:
                payload = json.loads(line[6:])
            except ValueError:
                # ValueError, not JSONDecodeError: non-UTF8 bytes raise
                # UnicodeDecodeError, and this runs inside the streaming loop
                # where anything raised truncates the client's response.
                continue
            if not isinstance(payload, dict) or payload.get("type") != "message_start":
                continue
            msg = payload.get("message")
            if not isinstance(msg, dict):
                continue
            record["usage"] = msg.get("usage")
            record["diagnostics"] = msg.get("diagnostics")
            record["diagnostics_present"] = "diagnostics" in msg
            record["response_id"] = msg.get("id")
            LAST_ID[_lineage_key(record.get("request_body", {}))] = msg.get("id")
            return True
    return False


def _parse_json_body(buf: bytes, record: dict) -> None:
    """Non-streaming replies carry the same fields, just not as SSE.

    Without this the ledger silently loses usage for every non-streamed call.
    """
    try:
        msg = json.loads(buf)
    except ValueError:
        # JSONDecodeError and UnicodeDecodeError are both ValueError. Catching
        # only the former let a gzipped body raise straight out of the response
        # generator and truncate the client's stream — the observer breaking
        # the thing it observes.
        return
    if not isinstance(msg, dict) or msg.get("type") == "error":
        return
    record["usage"] = msg.get("usage")
    record["diagnostics"] = msg.get("diagnostics")
    record["diagnostics_present"] = "diagnostics" in msg
    record["response_id"] = msg.get("id")
    if msg.get("id"):
        LAST_ID[_lineage_key(record.get("request_body", {}))] = msg["id"]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
