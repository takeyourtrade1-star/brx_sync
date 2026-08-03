"""Dependency-free ASGI controls for request framing and response headers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
from typing import Any


class SecurityHeadersMiddleware:
    _HEADERS = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        (b"cache-control", b"no-store"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-site"),
    )

    def __init__(self, app: Callable[..., Awaitable[Any]], hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        is_local_test_page = scope.get("path", "").startswith("/static/") or scope.get(
            "path"
        ) == "/test"
        csp = (
            b"default-src 'self'; script-src 'self' 'unsafe-inline'; "
            b"style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            b"img-src 'self' data:; frame-ancestors 'none'"
            if is_local_test_page
            else b"default-src 'none'; frame-ancestors 'none'"
        )

        async def send_with_headers(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _value in headers}
                headers.extend(
                    (name, value) for name, value in self._HEADERS if name not in present
                )
                if self.hsts and b"strict-transport-security" not in present:
                    headers.append(
                        (
                            b"strict-transport-security",
                            b"max-age=31536000; includeSubDomains",
                        )
                    )
                if b"content-security-policy" not in present:
                    headers.append((b"content-security-policy", csp))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: Callable[..., Awaitable[Any]],
        max_bytes: int,
        max_messages: int = 1024,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.max_messages = max_messages

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        raw_headers = scope.get("headers", [])
        lengths = [
            value.strip()
            for key, value in raw_headers
            if key.lower() == b"content-length"
        ]
        transfer_encodings = [
            value.strip().lower()
            for key, value in raw_headers
            if key.lower() == b"transfer-encoding"
        ]
        if (
            len(lengths) > 1
            or len(transfer_encodings) > 1
            or (lengths and transfer_encodings)
        ):
            await self._send_json(send, 400, "Ambiguous request framing")
            return
        if transfer_encodings and transfer_encodings[0] != b"chunked":
            await self._send_json(send, 400, "Invalid Transfer-Encoding header")
            return
        if lengths:
            if len(lengths[0]) > 20 or not lengths[0].isdigit():
                await self._send_json(send, 400, "Invalid Content-Length header")
                return
            if int(lengths[0]) > self.max_bytes:
                await self._send_json(send, 413, "Request body too large")
                return

        bodyless_method = str(scope.get("method", "")).upper() in {"GET", "HEAD"}
        if bodyless_method and (
            transfer_encodings or (lengths and int(lengths[0]) > 0)
        ):
            await self._send_json(send, 413, "Request body too large")
            return

        received = 0
        messages = 0
        rejected = False

        async def limited_receive() -> dict:
            nonlocal received, messages, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                messages += 1
                body = message.get("body", b"")
                received += len(body)
                if (
                    messages > self.max_messages
                    or received > self.max_bytes
                    or (bodyless_method and len(body) > 0)
                ):
                    rejected = True
                    return {"type": "http.disconnect"}
            return message

        response_started = False

        async def guarded_send(message: dict) -> None:
            nonlocal response_started
            if rejected:
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not rejected:
                raise
        if rejected and not response_started:
            await self._send_json(send, 413, "Request body too large")

    @staticmethod
    async def _send_json(send: Callable, code: int, detail: str) -> None:
        body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
