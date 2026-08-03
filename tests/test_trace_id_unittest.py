"""Trace headers are bounded, canonical and unambiguous."""

import unittest
import uuid

from starlette.requests import Request

from app.core.dependencies import get_trace_id
from app.core.exception_handlers import get_trace_id as get_exception_trace_id


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "query_string": b"",
            "client": ("127.0.0.1", 1234),
            "server": ("sync", 8002),
            "scheme": "http",
        }
    )


def _is_uuid(value: str) -> bool:
    return str(uuid.UUID(value)) == value


class TraceIdSecurityTests(unittest.TestCase):
    def test_safe_ascii_trace_id_is_preserved(self) -> None:
        request = _request([(b"x-trace-id", b"edge.req-42:part_1")])
        self.assertEqual(get_trace_id(request), "edge.req-42:part_1")
        self.assertEqual(get_exception_trace_id(request), "edge.req-42:part_1")

    def test_untrusted_values_are_replaced(self) -> None:
        for value in (
            b"bad\nlog-injection",
            "unicode-è".encode("utf-8"),
            b"a" * 129,
            b"",
        ):
            with self.subTest(value=value[:20]):
                self.assertTrue(_is_uuid(get_trace_id(_request([(b"x-trace-id", value)]))))

    def test_duplicate_or_aliased_headers_are_replaced(self) -> None:
        requests = (
            _request(
                [
                    (b"x-trace-id", b"first"),
                    (b"x-trace-id", b"second"),
                ]
            ),
            _request(
                [
                    (b"x-trace-id", b"first"),
                    (b"x-request-id", b"second"),
                ]
            ),
        )
        for request in requests:
            self.assertTrue(_is_uuid(get_trace_id(request)))

    def test_missing_header_is_server_generated(self) -> None:
        self.assertTrue(_is_uuid(get_trace_id(_request([]))))


if __name__ == "__main__":
    unittest.main()
