import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest

from app.models.inventory import UserInventoryItem
from app.services import cardtrader_mutation_lease as lease_module
from app.services.cardtrader_client import CardTraderAPIError
from app.services.cardtrader_mutation_lease import (
    CardTraderMutationBusyError,
    cardtrader_mutation_lease,
)
from app.services.cardtrader_payloads import build_product_update_payload
from app.services.inventory_operations import _authoritative_remote_quantity
from app.services.reconciler import validate_snapshot
from app.tasks import outbox_tasks
from app.tasks.outbox_tasks import (
    _job_has_errors,
    _mutation_matches_product,
    _payload_matches_product,
)


def _inventory_item(**properties):
    return UserInventoryItem(
        user_id=uuid.uuid4(),
        blueprint_id=10,
        quantity=2,
        price_cents=350,
        external_stock_id="123",
        source="cardtrader",
        environment="real",
        properties=properties,
    )


def test_boolean_strings_are_not_coerced_with_python_truthiness():
    payload = build_product_update_payload(
        _inventory_item(signed="false", altered="0", mtg_foil="no")
    )

    assert payload["properties"]["signed"] is False
    assert payload["properties"]["altered"] is False
    assert payload["properties"]["mtg_foil"] is False


def test_invalid_boolean_payload_fails_closed():
    with pytest.raises(ValueError, match="Invalid boolean"):
        build_product_update_payload(_inventory_item(signed="sometimes"))


def test_completed_job_with_item_error_is_not_success():
    assert _job_has_errors(
        {
            "state": "completed",
            "stats": {"ok": 0, "warning": 0, "error": 1},
            "results": [{"result": "error", "errors": {"id": ["not found"]}}],
        }
    )


def test_completed_job_with_warning_only_is_accepted():
    assert not _job_has_errors(
        {
            "state": "completed",
            "stats": {"ok": 0, "warning": 1, "error": 0},
            "results": [{"result": "warning"}],
        }
    )


def test_export_must_match_all_requested_fields_before_verification():
    payload = {
        "id": 123,
        "quantity": 2,
        "price": 3.5,
        "properties": {"signed": False},
    }
    product = {
        "id": 123,
        "quantity": 2,
        "price": {"cents": 350},
        "properties_hash": {"signed": False, "condition": "Near Mint"},
    }

    assert _payload_matches_product(payload, product)
    assert not _payload_matches_product(payload, {**product, "quantity": 1})


def test_post_job_verification_accepts_only_matching_product_or_zero_absence():
    payload = {"id": 123, "quantity": 2, "price": 3.5}
    matching = {"id": 123, "quantity": 2, "price_cents": 350}

    assert _mutation_matches_product("update_product", payload, matching)
    assert not _mutation_matches_product(
        "update_product",
        payload,
        {**matching, "quantity": 1},
    )
    assert not _mutation_matches_product("update_product", payload, None)
    assert _mutation_matches_product(
        "update_product",
        {**payload, "quantity": 0},
        None,
    )


def test_create_verification_requires_exact_marker_identity_and_stock():
    payload = {
        "blueprint_id": 42,
        "quantity": 3,
        "price": 2.0,
        "description": "Native Ebartex listing",
        "user_data_field": "ebartex_listing:listing-1",
        "graded": False,
        "properties": {
            "condition": "Near Mint",
            "mtg_language": "en",
        },
    }
    product = {
        "id": 999,
        "blueprint_id": 42,
        "quantity": 3,
        "price": {"cents": 200, "currency": "EUR"},
        "description": "Native Ebartex listing",
        "user_data_field": "ebartex_listing:listing-1",
        "graded": False,
        "properties": {
            "condition": "Near Mint",
            "mtg_language": "en",
            "signed": False,
        },
    }

    assert _mutation_matches_product("create_product", payload, product)
    assert not _mutation_matches_product(
        "create_product",
        payload,
        {**product, "user_data_field": "another-listing"},
    )
    assert not _mutation_matches_product(
        "create_product",
        payload,
        {**product, "blueprint_id": 43},
    )
    assert not _mutation_matches_product("create_product", payload, None)


def test_increment_quantity_accepts_omitted_identity_but_validates_it_when_present():
    assert (
        _authoritative_remote_quantity(
            {"resource": {"quantity": 2}},
            expected_product_id=123,
        )
        == 2
    )
    assert (
        _authoritative_remote_quantity(
            {
                "id": "123",
                "game_id": 1,
                "resource": {
                    "id": 123,
                    "game_id": "1",
                    "quantity": 2,
                },
            },
            expected_product_id=123,
        )
        == 2
    )

    with pytest.raises(CardTraderAPIError, match="different product") as wrong_id:
        _authoritative_remote_quantity(
            {"resource": {"id": 999, "quantity": 2}},
            expected_product_id=123,
        )
    assert wrong_id.value.outcome_unknown is True

    with pytest.raises(CardTraderAPIError, match="not for Magic") as wrong_game:
        _authoritative_remote_quantity(
            {"game_id": 2, "quantity": 2},
            expected_product_id=123,
        )
    assert wrong_game.value.outcome_unknown is True


def test_snapshot_rejects_duplicates_and_implausible_truncation():
    duplicate = [
        {
            "id": 1,
            "game_id": 1,
            "blueprint_id": 10,
            "quantity": 1,
            "price_cents": 100,
        },
        {
            "id": 1,
            "game_id": 1,
            "blueprint_id": 10,
            "quantity": 1,
            "price_cents": 100,
        },
    ]
    ok, problems = validate_snapshot(duplicate, previous_snapshot_size=None)
    assert not ok
    assert any("duplicato" in problem for problem in problems)

    truncated = [
        {
            "id": index,
            "game_id": 1,
            "blueprint_id": index + 1,
            "quantity": 1,
            "price_cents": 100,
        }
        for index in range(4)
    ]
    ok, problems = validate_snapshot(truncated, previous_snapshot_size=20)
    assert not ok
    assert any("implausibil" in problem for problem in problems)


@pytest.mark.asyncio
async def test_cardtrader_mutations_are_serialized_per_user(monkeypatch):
    user_id = uuid.uuid4()

    class FakeRedis:
        def __init__(self):
            self.values = {}

        def set(self, key, value, *, nx, ex):
            assert nx is True
            assert ex == lease_module.LEASE_SECONDS
            if key in self.values:
                return False
            self.values[key] = value
            return True

        def eval(self, script, _keys, key, owner, *_args):
            if "expire" in script:
                return int(self.values.get(key) == owner)
            if self.values.get(key) != owner:
                return 0
            del self.values[key]
            return 1

    redis = FakeRedis()
    monkeypatch.setattr(lease_module, "get_redis_sync", lambda: redis)

    async with cardtrader_mutation_lease(user_id) as lease:
        lease.refresh()
        with pytest.raises(CardTraderMutationBusyError):
            async with cardtrader_mutation_lease(user_id):
                pass

    async with cardtrader_mutation_lease(user_id) as lease:
        lease.refresh()


@pytest.mark.asyncio
async def test_cardtrader_mutation_lease_heartbeats_while_body_is_running(
    monkeypatch,
):
    class FakeRedis:
        def __init__(self):
            self.owner = None
            self.refreshes = 0

        def set(self, _key, owner, *, nx, ex):
            self.owner = owner
            return nx and ex == lease_module.LEASE_SECONDS

        def eval(self, script, _keys, _key, owner, *_args):
            if "expire" in script:
                self.refreshes += 1
                return int(owner == self.owner)
            self.owner = None
            return 1

    redis = FakeRedis()
    monkeypatch.setattr(lease_module, "LEASE_SECONDS", 0.15)
    monkeypatch.setattr(lease_module, "get_redis_sync", lambda: redis)

    async with cardtrader_mutation_lease(uuid.uuid4()):
        await asyncio.sleep(0.12)

    assert redis.refreshes >= 1


@pytest.mark.asyncio
async def test_prewrite_stock_mismatch_waits_for_validated_full_export(
    monkeypatch,
):
    command_id = uuid.uuid4()
    user_id = uuid.uuid4()
    state_updates = []

    class FakeLease:
        def refresh(self):
            return None

    @asynccontextmanager
    async def fake_lease(_user_id):
        yield FakeLease()

    class FakeClient:
        def __init__(self, _token, _user_id):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get_product(self, _product_id):
            return None

        async def bulk_update_products(self, _payload):
            raise AssertionError("pre-write mismatch must not mutate CardTrader")

    async def fake_claim(_command_id):
        return {
            "id": command_id,
            "user_id": user_id,
            "operation_type": "update_product",
            "target_product_id": "123",
            "payload": {"id": 123, "quantity": 2, "price": 3.5},
            "token": "token",
            "job_uuid": None,
            "context": {"old_quantity": 1},
            "mode_version": 1,
        }

    async def always_valid(_command_id):
        return True

    async def capture_state(_command_id, status, **kwargs):
        state_updates.append((status, kwargs))

    async def forbidden_partial_resolution(*_args, **_kwargs):
        raise AssertionError("single-resource payload must not drive recovery")

    monkeypatch.setattr(outbox_tasks, "_claim_command", fake_claim)
    monkeypatch.setattr(
        outbox_tasks,
        "_revalidate_claim_before_remote_write",
        always_valid,
    )
    monkeypatch.setattr(outbox_tasks, "cardtrader_mutation_lease", fake_lease)
    monkeypatch.setattr(outbox_tasks, "CardTraderClient", FakeClient)
    monkeypatch.setattr(outbox_tasks, "_update_command_state", capture_state)
    monkeypatch.setattr(
        outbox_tasks,
        "resolve_uncertain_command_from_export",
        forbidden_partial_resolution,
    )

    result = await outbox_tasks._process_command(command_id)

    assert result["status"] == "uncertain"
    assert "validated full export" in result["reason"]
    assert state_updates == [
        (
            "uncertain",
            {
                "error": (
                    "CardTrader stock changed before mutation; " "awaiting validated full export"
                )
            },
        )
    ]
