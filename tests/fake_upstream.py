"""Anthropic-shaped SSE upstream, so proxy mechanics can be tested offline."""
import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI()
STATE = {"last_headers": None, "last_body": None, "cancelled": False, "events_sent": 0}
CHUNK_DELAY = 0.15


@app.get("/_last")
async def last():
    return JSONResponse(STATE)


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    # Behaviour switches, so the proxy can be tested against non-200 and
    # non-streaming replies as well as the happy path.
    if body.get("model") == "error-400":
        return JSONResponse(status_code=400, content={
            "type": "error", "error": {"type": "invalid_request_error",
                                       "message": "simulated upstream rejection"}})
    # Only honours identity; if the proxy fails to ask for it, this returns
    # gzip and the proxy's parsing must still cope.
    # An uncooperative upstream: compresses no matter what was asked for.
    if body.get("model") == "gzip-always":
        import gzip as _gz
        payload = (b'event: message_start\ndata: ' + json.dumps({
            "type": "message_start",
            "message": {"id": "msg_gz2", "type": "message", "role": "assistant",
                        "content": [], "usage": {"input_tokens": 5, "output_tokens": 1,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
        }).encode() + b'\n\n')
        return Response(content=_gz.compress(payload), media_type="text/event-stream",
                        headers={"content-encoding": "gzip"})
    if body.get("model") == "gzip-me":
        import gzip as _gz
        payload = (b'event: message_start\ndata: ' + json.dumps({
            "type": "message_start",
            "message": {"id": "msg_gz", "type": "message", "role": "assistant",
                        "content": [], "usage": {"input_tokens": 11, "output_tokens": 2,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
        }).encode() + b'\n\n')
        wants_identity = request.headers.get("accept-encoding", "") == "identity"
        if wants_identity:
            return Response(content=payload, media_type="text/event-stream")
        return Response(content=_gz.compress(payload), media_type="text/event-stream",
                        headers={"content-encoding": "gzip"})
    if body.get("stream") is False:
        return JSONResponse({"id": "msg_fake_json", "type": "message",
                             "role": "assistant", "content": [],
                             "usage": {"input_tokens": 7, "output_tokens": 3,
                                       "cache_read_input_tokens": 0,
                                       "cache_creation_input_tokens": 0}})
    STATE.update(last_headers=dict(request.headers), last_body=body,
                 cancelled=False, events_sent=0)

    diag = None
    if (body.get("diagnostics") or {}).get("previous_message_id"):
        diag = {"cache_miss_reason": {"type": "system_changed",
                                      "cache_missed_input_tokens": 41850}}

    async def gen():
        try:
            start = {
                "type": "message_start",
                "message": {
                    "id": "msg_fake_01", "type": "message", "role": "assistant",
                    "content": [],
                    "usage": {"input_tokens": 42, "cache_read_input_tokens": 1200,
                              "cache_creation_input_tokens": 0, "output_tokens": 7},
                    "diagnostics": diag,
                },
            }
            yield f"event: message_start\ndata: {json.dumps(start)}\n\n".encode()
            STATE["events_sent"] += 1
            for i in range(5):
                await asyncio.sleep(CHUNK_DELAY)
                d = {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": f"chunk{i} "}}
                yield f"event: content_block_delta\ndata: {json.dumps(d)}\n\n".encode()
                STATE["events_sent"] += 1
            await asyncio.sleep(CHUNK_DELAY)
            yield b'event: message_stop\ndata: {"type": "message_stop"}\n\n'
            STATE["events_sent"] += 1
        except asyncio.CancelledError:
            STATE["cancelled"] = True
            raise

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8801, log_level="warning")
