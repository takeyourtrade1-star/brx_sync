"""Regression tests for preserving valid CardTrader rows awaiting catalog mapping."""

from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect

from app.api.v1.routes.sync import get_listings_by_blueprint
from app.services import reconciler


def _magic_product(
    product_id: int,
    blueprint_id: int,
    *,
    quantity: int = 3,
) -> dict:
    return {
        "id": product_id,
        "game_id": 1,
        "blueprint_id": blueprint_id,
        "quantity": quantity,
        "price_cents": 125,
        "category_id": 1,
        "name_en": "Pending card",
        "image_url": "https://cdn.cardtrader.com/card.jpg",
        "expansion": {"id": 4415, "name": "Example set", "code": "EX"},
        "uploaded_images": ["https://cdn.cardtrader.com/uploaded.jpg"],
        "properties_hash": {"condition": "Near Mint", "signed": False},
    }


def test_missing_blueprint_keeps_row_and_copy_quantity() -> None:
    normalized, problems = reconciler.normalize_magic_snapshot(
        [_magic_product(10, 20), _magic_product(11, 21, quantity=46)]
    )

    mapped, unmapped, mapping_problems, unsupported = reconciler._classify_magic_products(
        normalized,
        lambda blueprint_id: (blueprint_id, "cards_prints") if blueprint_id == 20 else None,
    )

    assert problems == []
    assert mapping_problems == []
    assert [row["id"] for row in mapped] == ["10"]
    assert [row["id"] for row in unmapped] == ["11"]
    assert unmapped[0]["_mapping_status"] == "missing"
    assert unmapped[0]["quantity"] == 46
    assert unsupported == 1

    metrics = reconciler._snapshot_metrics(normalized, mapped, unmapped)
    assert metrics == {
        "raw_rows": 2,
        "raw_copies": 49,
        "imported_rows": 1,
        "imported_copies": 3,
        "unmapped_rows": 1,
        "unmapped_copies": 46,
        "quarantined_rows": 0,
        "quarantined_copies": 0,
        "incomplete": True,
    }


def test_pending_metadata_preserves_bounded_cardtrader_identity() -> None:
    product = _magic_product(10, 20, quantity=46)

    metadata = reconciler._bounded_catalog_metadata(product)
    properties = reconciler._properties_for_unmapped_product(product)

    assert metadata["blueprint_id"] == 20
    assert metadata["quantity"] == 46
    assert metadata["name_en"] == "Pending card"
    assert metadata["expansion"] == {"id": 4415, "name": "Example set", "code": "EX"}
    assert metadata["uploaded_images"] == ["https://cdn.cardtrader.com/uploaded.jpg"]
    assert properties["condition"] == "Near Mint"
    assert properties[reconciler.CATALOG_METADATA_KEY] == metadata
    assert "token" not in metadata


@pytest.mark.asyncio
async def test_unmapped_persistence_passes_explicit_environment_to_catalog_queue(monkeypatch) -> None:
    queue = AsyncMock()
    monkeypatch.setattr(reconciler, "enqueue_catalog_import", queue)
    product = {**_magic_product(10, 20), "environment": "real"}
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
    )
    user_id = uuid.uuid4()

    result = await reconciler._persist_unmapped_product(
        session,
        product={**product, "_mapping_status": "missing"},
        local=None,
        user_id=user_id,
        environment="partial",
        watermark=0,
        snapshot_id=uuid.uuid4(),
    )

    queue.assert_awaited_once()
    args = queue.await_args.args
    assert args[0] is session
    assert args[1] == user_id
    assert args[2]["environment"] == "partial"
    assert args[2]["external_stock_id"] == "10"
    assert args[2]["catalog_metadata"]["expansion"]["id"] == 4415
    assert result["unmapped_created"] == 1


def test_quarantine_metrics_exclude_current_pending_cards() -> None:
    pending = SimpleNamespace(
        source="cardtrader",
        game_id=1,
        lifecycle_status="active",
        sync_state="synced",
        sync_uncertain_event_id=None,
        quantity=46,
        mapping_status="missing",
    )
    legacy = SimpleNamespace(
        source="cardtrader",
        game_id=None,
        lifecycle_status="stale",
        sync_state="uncertain",
        sync_uncertain_event_id=4,
        quantity=1,
        mapping_status="unsupported",
    )

    metrics = reconciler._snapshot_metrics(
        [_magic_product(10, 20, quantity=46)],
        [],
        [_magic_product(10, 20, quantity=46)],
        [pending, legacy],
    )

    assert metrics["unmapped_copies"] == 46
    assert metrics["quarantined_rows"] == 1
    assert metrics["quarantined_copies"] == 1
    assert metrics["incomplete"] is True


@pytest.mark.asyncio
async def test_missing_current_magic_is_zeroed_without_catalog_mapper() -> None:
    statements = []

    class Session:
        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(rowcount=1)

    current = SimpleNamespace(
        id=uuid.uuid4(),
        external_stock_id="ct-10",
        game_id=1,
        blueprint_id=999999,
        missing_snapshot_count=0,
        row_version=3,
        reserved_quantity=0,
    )
    mapper = Mock(return_value=None)

    first = await reconciler._apply_missing_products(
        Session(),
        local_items=[current],
        present_ids=set(),
        map_blueprint=mapper,
        user_id=uuid.uuid4(),
        environment="real",
        watermark=0,
    )
    current.missing_snapshot_count = 1
    second = await reconciler._apply_missing_products(
        Session(),
        local_items=[current],
        present_ids=set(),
        map_blueprint=mapper,
        user_id=uuid.uuid4(),
        environment="real",
        watermark=0,
    )

    assert first["missing_quarantined"] == 1
    assert second["archived"] == 1
    mapper.assert_not_called()
    assert len(statements) == 2


@pytest.mark.asyncio
async def test_legacy_null_game_id_uses_mapper_only_for_magic_repair() -> None:
    statements = []

    class Session:
        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(rowcount=1)

    legacy = SimpleNamespace(
        id=uuid.uuid4(),
        external_stock_id="legacy-10",
        game_id=None,
        blueprint_id=20,
        missing_snapshot_count=1,
        row_version=3,
        reserved_quantity=0,
    )
    mapper = Mock(return_value=(64048, "cards_prints"))

    result = await reconciler._apply_missing_products(
        Session(),
        local_items=[legacy],
        present_ids=set(),
        map_blueprint=mapper,
        user_id=uuid.uuid4(),
        environment="real",
        watermark=0,
    )

    assert result["archived"] == 1
    mapper.assert_called_once_with(20)
    compiled = statements[0].compile()
    params = compiled.params
    assert params["game_id"] == 1
    assert "CASE" in str(compiled).upper()
    assert "mapped" in params.values()
    assert "missing" in params.values()


@pytest.mark.asyncio
async def test_public_blueprint_listings_require_mapped_catalog(monkeypatch) -> None:
    statements = []

    class Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        async def execute(self, statement):
            statements.append(statement)
            return Result()

    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: SimpleNamespace(CARDTRADER_WRITES_ENABLED=True),
    )

    response = await get_listings_by_blueprint(393523, limit=100, session=Session())

    assert response.listings == []
    sql = str(statements[0].compile(dialect=postgresql_dialect()))
    assert "mapping_status" in sql
