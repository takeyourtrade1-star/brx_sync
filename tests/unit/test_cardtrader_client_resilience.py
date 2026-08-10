import json
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import circuit_breaker as circuit_breaker_module
from app.core.exceptions import RateLimitError
from app.services.cardtrader_client import CardTraderAPIError, CardTraderClient
from app.services.circuit_breaker import CircuitState


class _Response:
    def __init__(
        self,
        status_code,
        payload=None,
        *,
        headers=None,
        invalid_json=False,
        chunks=None,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False
        self.chunks_consumed = 0
        if chunks is not None:
            self._chunks = chunks
        elif invalid_json:
            self._chunks = [b"{invalid-json"]
        else:
            self._chunks = [json.dumps(payload).encode("utf-8")]

    async def aiter_raw(self):
        for chunk in self._chunks:
            self.chunks_consumed += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class _Circuit:
    def __init__(self, *, telemetry_error=False):
        self.failures = []
        self.successes = 0
        self.telemetry_error = telemetry_error

    def get_state(self):
        return CircuitState.CLOSED

    def record_failure(self, error_type):
        self.failures.append(error_type)

    def record_success(self):
        self.successes += 1
        if self.telemetry_error:
            raise RuntimeError("redis unavailable")


class _Adaptive:
    def __init__(self, *, telemetry_error=False):
        self.telemetry_error = telemetry_error

    def record_429_response(self, _user_id):
        return None

    def record_success(self, _user_id):
        if self.telemetry_error:
            raise RuntimeError("redis unavailable")


def _client_with_response(response, *, telemetry_error=False):
    client = object.__new__(CardTraderClient)
    client.user_id = "seller-1"
    client.base_url = "https://cardtrader.invalid"
    client.client = AsyncMock()
    client.client.build_request = Mock(return_value=object())
    client.client.send = AsyncMock(return_value=response)
    client._wait_for_rate_limit = AsyncMock()
    client.circuit_breaker = _Circuit(telemetry_error=telemetry_error)
    client.adaptive_rate_limiter = _Adaptive(telemetry_error=telemetry_error)
    return client


@pytest.mark.asyncio
async def test_single_product_create_uses_strict_cardtrader_v2_endpoint():
    client = object.__new__(CardTraderClient)
    client._make_request = AsyncMock(
        return_value={"result": "ok", "resource": {"id": 10}}
    )
    payload = {
        "blueprint_id": 42,
        "price": 2.0,
        "quantity": 3,
        "error_mode": "strict",
        "user_data_field": "ebartex_listing:listing-1",
        "properties": {"condition": "Near Mint", "mtg_language": "en"},
        "graded": False,
    }

    result = await client.create_product(payload)

    assert result["resource"]["id"] == 10
    client._make_request.assert_awaited_once_with("POST", "/products", json=payload)


@pytest.mark.asyncio
async def test_single_product_create_rejects_unbounded_or_unknown_fields():
    client = object.__new__(CardTraderClient)
    client._make_request = AsyncMock()

    with pytest.raises(ValueError, match="Unsupported"):
        await client.create_product(
            {
                "blueprint_id": 42,
                "price": 2.0,
                "quantity": 1,
                "admin": True,
            }
        )

    client._make_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_write_with_invalid_json_is_uncertain_and_not_retried():
    client = _client_with_response(_Response(202, invalid_json=True))

    with pytest.raises(CardTraderAPIError) as caught:
        await client._make_request("POST", "/products/bulk_update", json={})

    assert caught.value.outcome_unknown is True
    assert client.client.send.await_count == 1
    assert client.client.send.await_args.kwargs == {"stream": True}
    assert client.client.send.return_value.closed is True


@pytest.mark.asyncio
async def test_telemetry_failure_cannot_mask_remote_success():
    client = _client_with_response(
        _Response(200, {"resource": {"id": 10}}),
        telemetry_error=True,
    )

    result = await client._make_request("POST", "/products/bulk_update", json={})

    assert result == {"resource": {"id": 10}}
    assert client.client.send.await_count == 1
    assert client.client.send.return_value.closed is True


@pytest.mark.asyncio
async def test_streaming_response_over_limit_fails_closed_without_full_buffer(monkeypatch):
    from app.services import cardtrader_client as client_module

    monkeypatch.setattr(client_module.settings, "CARDTRADER_MAX_RESPONSE_BYTES", 16)
    response = _Response(
        200,
        chunks=[b'{"value":"', b"x" * 8, b"never-consumed"],
    )
    client = _client_with_response(response)

    with pytest.raises(CardTraderAPIError, match="exceeded the configured limit"):
        await client._make_request("POST", "/products/bulk_update", json={})

    assert response.chunks_consumed == 2
    assert response.closed is True
    assert client.client.send.await_count == 1


@pytest.mark.asyncio
async def test_compressed_response_is_rejected_before_decompression():
    response = _Response(
        200,
        headers={"Content-Encoding": "gzip"},
        chunks=[b"compressed-body-must-not-be-consumed"],
    )
    client = _client_with_response(response)

    with pytest.raises(CardTraderAPIError, match="unsupported Content-Encoding"):
        await client._make_request("GET", "/products/export")

    assert response.chunks_consumed == 0
    assert response.closed is True


@pytest.mark.asyncio
async def test_chunked_response_without_content_length_is_bounded_and_parsed(monkeypatch):
    from app.services import cardtrader_client as client_module

    monkeypatch.setattr(client_module.settings, "CARDTRADER_MAX_RESPONSE_BYTES", 64)
    response = _Response(
        200,
        headers={"Transfer-Encoding": "chunked"},
        chunks=[b'{"resource":', b'{"id": 10}}'],
    )
    client = _client_with_response(response)

    result = await client._make_request("GET", "/products/10")

    assert result == {"resource": {"id": 10}}
    assert response.chunks_consumed == 2
    assert response.closed is True


@pytest.mark.asyncio
async def test_valid_content_length_response_is_streamed_and_parsed(monkeypatch):
    from app.services import cardtrader_client as client_module

    body = b'{"resource":{"id":10}}'
    monkeypatch.setattr(client_module.settings, "CARDTRADER_MAX_RESPONSE_BYTES", len(body))
    response = _Response(
        200,
        headers={"Content-Length": str(len(body))},
        chunks=[body[:7], body[7:]],
    )
    client = _client_with_response(response)

    result = await client._make_request("GET", "/products/10")

    assert result == {"resource": {"id": 10}}
    assert response.chunks_consumed == 2
    assert response.closed is True


@pytest.mark.asyncio
async def test_delete_uses_status_code_only_for_already_deleted():
    client = object.__new__(CardTraderClient)
    client._make_request = AsyncMock(
        side_effect=CardTraderAPIError(
            "server body happens to contain 404",
            status_code=500,
            outcome_unknown=True,
        )
    )

    with pytest.raises(CardTraderAPIError):
        await client.delete_product(10)

    client._make_request = AsyncMock(side_effect=CardTraderAPIError("not found", status_code=404))
    result = await client.delete_product(10)
    assert result["status"] == "already_deleted"


def test_circuit_breaker_is_scoped_per_cardtrader_account():
    circuit_breaker_module._circuit_breakers.clear()

    first = circuit_breaker_module.get_circuit_breaker("seller-a")
    second = circuit_breaker_module.get_circuit_breaker("seller-b")

    assert first is not second
    assert first.circuit_key.endswith(":seller-a")
    assert second.circuit_key.endswith(":seller-b")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    ["https://attacker.invalid/steal", "//attacker.invalid/steal", "/ok\\bad"],
)
async def test_absolute_or_ambiguous_endpoints_are_rejected(endpoint):
    client = _client_with_response(_Response(200, {}))

    with pytest.raises(CardTraderAPIError):
        await client._make_request("GET", endpoint)

    client.client.build_request.assert_not_called()
    client.client.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", ["999999999", "inf", "nan", "invalid"])
async def test_retry_after_cannot_suspend_worker_beyond_configured_cap(
    retry_after,
    monkeypatch,
):
    from app.services import cardtrader_client as client_module

    responses = [
        _Response(429, {}, headers={"Retry-After": retry_after})
        for _ in range(3)
    ]
    client = _client_with_response(responses[0])
    client.client.send = AsyncMock(side_effect=responses)
    sleep = AsyncMock()
    monkeypatch.setattr(client_module.settings, "CARDTRADER_MAX_RETRY_AFTER_SECONDS", 3.0)
    monkeypatch.setattr(client_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(client_module.random, "uniform", lambda _start, _end: 0.0)

    with pytest.raises(RateLimitError):
        await client._make_request("GET", "/products/export")

    waits = [call.args[0] for call in sleep.await_args_list]
    assert waits == [5.0, 7.0]
