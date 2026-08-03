from __future__ import annotations

import pytest
from fastapi import FastAPI, Request

from app.core.http_security import RequestBodyLimitMiddleware


@pytest.mark.asyncio
async def test_real_fastapi_route_chunked_overflow_returns_one_413_and_stops_reading():
    application = FastAPI()

    @application.post("/ingest")
    async def ingest(request: Request) -> dict[str, bool]:
        await request.body()
        return {"accepted": True}

    application.add_middleware(RequestBodyLimitMiddleware, max_bytes=4)
    chunks = [
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"45", "more_body": True},
        {"type": "http.request", "body": b"not-consumed", "more_body": False},
    ]
    reads = 0
    sent = []

    async def receive():
        nonlocal reads
        message = chunks[reads]
        reads += 1
        return message

    async def send(message):
        sent.append(message)

    await application(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/ingest",
            "raw_path": b"/ingest",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"transfer-encoding", b"chunked")],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        },
        receive,
        send,
    )

    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert reads == 2
    assert len(starts) == 1
    assert starts[0]["status"] == 413
