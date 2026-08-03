"""Canonical parsing for untrusted distributed tracing headers."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable

from starlette.requests import Request


_TRACE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def safe_trace_id(
    request: Request,
    accepted_headers: Iterable[str] = (
        "x-trace-id",
        "x-request-id",
        "x-correlation-id",
    ),
) -> str:
    """Return one unambiguous ASCII trace id or a server-generated UUID."""

    accepted = {name.lower().encode("ascii") for name in accepted_headers}
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        values = [
            value
            for name, value in scope.get("headers", ())
            if bytes(name).lower() in accepted
        ]
    else:
        # Compatibility for direct unit use. Production Starlette requests
        # always take the raw branch above, where duplicates remain visible.
        header_map = getattr(request, "headers", {})
        try:
            values = [
                str(value).encode("ascii", errors="strict")
                for name in accepted_headers
                if (value := header_map.get(name)) is not None
            ]
        except UnicodeEncodeError:
            return str(uuid.uuid4())
    # Multiple aliases or repeated fields are ambiguous across proxies. Never
    # pick first/last because downstream components may make a different choice.
    if len(values) != 1:
        return str(uuid.uuid4())
    try:
        candidate = bytes(values[0]).decode("ascii")
    except UnicodeDecodeError:
        return str(uuid.uuid4())
    if _TRACE_ID.fullmatch(candidate) is None:
        return str(uuid.uuid4())
    return candidate
