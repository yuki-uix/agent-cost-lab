"""Anthropic-shaped SSE upstream, so proxy mechanics can be tested offline."""
import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
STATE = {"last_headers": None, "last_body": None, "cancelled": False, "events_sent": 0}
CHUNK_DELAY = 0.15


@app.get("/_last")
async def last():
    return JSONResponse(STATE)


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
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
