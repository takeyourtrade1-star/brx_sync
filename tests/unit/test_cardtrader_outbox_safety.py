import uuid

import pytest

from app.models.inventory import UserInventoryItem
from app.services.cardtrader_mutation_lease import (
    CardTraderMutationBusyError,
    cardtrader_mutation_lease,
)
from app.services.cardtrader_payloads import build_product_update_payload
from app.services.reconciler import validate_snapshot
from app.tasks.outbox_tasks import _job_has_errors, _payload_matches_product


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


def test_snapshot_rejects_duplicates_and_implausible_truncation():
    duplicate = [{"id": 1, "quantity": 1}, {"id": 1, "quantity": 1}]
    ok, problems = validate_snapshot(duplicate, previous_snapshot_size=None)
    assert not ok
    assert any("duplicati" in problem for problem in problems)

    truncated = [{"id": index, "quantity": 1} for index in range(4)]
    ok, problems = validate_snapshot(truncated, previous_snapshot_size=20)
    assert not ok
    assert any("implausibile" in problem for problem in problems)


@pytest.mark.asyncio
async def test_cardtrader_mutations_are_serialized_per_user():
    user_id = uuid.uuid4()

    async with cardtrader_mutation_lease(user_id) as lease:
        lease.refresh()
        with pytest.raises(CardTraderMutationBusyError):
            async with cardtrader_mutation_lease(user_id):
                pass

    async with cardtrader_mutation_lease(user_id) as lease:
        lease.refresh()
