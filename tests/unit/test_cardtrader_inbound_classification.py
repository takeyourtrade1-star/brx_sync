"""Unit coverage for fail-safe CardTrader webhook and snapshot semantics."""

from datetime import datetime, timedelta, timezone

from app.services import reconciler
from app.services.webhook_ledger_processor import classify_order_webhook
from app.services.webhook_processor import (
    WebhookLedgerProcessor,
    WebhookProcessor,
)


def _magic_product(product_id: int = 10, blueprint_id: int = 20) -> dict:
    return {
        "id": product_id,
        "game_id": 1,
        "blueprint_id": blueprint_id,
        "quantity": 3,
        "price_cents": 125,
    }


def test_legacy_webhook_import_is_only_safe_ledger_alias() -> None:
    assert WebhookProcessor is WebhookLedgerProcessor


def test_standard_paid_order_requires_snapshot_without_delta() -> None:
    decision = classify_order_webhook(
        {
            "cause": "order.create",
            "data": {
                "id": 7,
                "state": "paid",
                "via_cardtrader_zero": False,
                "order_items": [{"product_id": 10, "quantity": 2}],
            },
        }
    )

    assert decision.requires_reconcile is True
    assert decision.reason == "standard_paid_order"
    assert decision.items == ({"product_id": "10", "quantity": 2},)


def test_zero_paid_is_verify_only_and_hub_pending_filters_presale_items() -> None:
    paid = classify_order_webhook(
        {
            "cause": "order.update",
            "data": {
                "id": "weekly",
                "state": "paid",
                "via_cardtrader_zero": True,
                "order_items": [{"product_id": 10, "quantity": 2}],
            },
        }
    )
    hub_pending = classify_order_webhook(
        {
            "cause": "order.update",
            "data": {
                "id": "hub-1",
                "state": "hub_pending",
                "via_cardtrader_zero": True,
                "presale": True,
                "order_items": [
                    {
                        "product_id": 10,
                        "quantity": 1,
                        "hub_pending_order_id": "hub-1",
                    },
                    {
                        "product_id": 11,
                        "quantity": 1,
                        "hub_pending_order_id": "other",
                    },
                ],
            },
        }
    )

    assert paid.reason == "cardtrader_zero_paid_verify_only"
    assert paid.requires_reconcile is True
    assert [item["product_id"] for item in hub_pending.items] == ["10"]

    mismatched_presale = classify_order_webhook(
        {
            "cause": "order.update",
            "data": {
                "id": "hub-2",
                "state": "hub_pending",
                "via_cardtrader_zero": True,
                "presale": True,
                "order_items": [
                    {
                        "product_id": 11,
                        "quantity": 1,
                        "hub_pending_order_id": "other",
                    }
                ],
            },
        }
    )
    assert mismatched_presale.items == ()
    assert mismatched_presale.full_quarantine is True


def test_cancel_destroy_and_unknown_state_fail_closed() -> None:
    cancel = classify_order_webhook(
        {
            "cause": "order.update",
            "data": {"id": 4, "state": "request_for_cancel"},
        }
    )
    destroy = classify_order_webhook({"cause": "order.destroy", "object_id": 4})
    unknown = classify_order_webhook(
        {"cause": "order.update", "data": {"id": 4, "state": "new_state"}}
    )

    assert cancel.requires_reconcile and cancel.full_quarantine
    assert destroy.requires_reconcile and destroy.full_quarantine
    assert unknown.requires_reconcile and unknown.full_quarantine


def test_magic_snapshot_rejects_ambiguous_rows_and_ignores_other_games() -> None:
    normalized, problems = reconciler.normalize_magic_snapshot(
        [
            _magic_product(),
            {
                "id": 99,
                "game_id": 2,
                "blueprint_id": 88,
                "quantity": 9,
                "price_cents": 500,
            },
        ]
    )
    assert problems == []
    assert [row["id"] for row in normalized] == ["10"]

    _normalized, missing_game = reconciler.normalize_magic_snapshot(
        [{key: value for key, value in _magic_product().items() if key != "game_id"}]
    )
    _normalized, negative = reconciler.normalize_magic_snapshot(
        [{**_magic_product(), "quantity": -1}]
    )
    _normalized, duplicate = reconciler.normalize_magic_snapshot(
        [_magic_product(), _magic_product()]
    )

    assert missing_game
    assert negative
    assert duplicate


def test_only_cards_prints_mapping_can_enter_magic_inventory() -> None:
    products, problems = reconciler.normalize_magic_snapshot(
        [_magic_product(10, 20), _magic_product(11, 21)]
    )

    mapped, mapping_problems, unsupported = reconciler._filter_cards_prints(
        products,
        lambda blueprint_id: (
            (blueprint_id, "cards_prints") if blueprint_id == 20 else (blueprint_id, "pk_prints")
        ),
    )

    assert problems == []
    assert mapping_problems == []
    assert [row["id"] for row in mapped] == ["10"]
    assert unsupported == 1


def test_large_shrink_is_confirmed_by_three_recent_stable_exports() -> None:
    now = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
    shrink_problem = ["set Magic implausibilmente ridotto: export=2033 baseline=2265 drop=232"]
    prior = [
        ("rejected", 2031, shrink_problem, now - timedelta(hours=6)),
        ("rejected", 2033, shrink_problem, now - timedelta(hours=12)),
    ]

    assert reconciler._has_stable_shrink_confirmation(prior, 2030, now=now)


def test_large_shrink_confirmation_stays_fail_closed_for_unstable_or_stale_exports() -> None:
    now = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
    problem = ["set Magic implausibilmente ridotto: export=2000 baseline=2265 drop=265"]

    assert not reconciler._has_stable_shrink_confirmation(
        [("rejected", 2030, problem, now - timedelta(hours=6))],
        2029,
        now=now,
    )
    assert not reconciler._has_stable_shrink_confirmation(
        [
            ("rejected", 2030, problem, now - timedelta(hours=6)),
            ("rejected", 1800, problem, now - timedelta(hours=12)),
        ],
        2029,
        now=now,
    )
    assert not reconciler._has_stable_shrink_confirmation(
        [
            ("rejected", 2030, problem, now - timedelta(hours=6)),
            ("rejected", 2029, problem, now - timedelta(hours=60)),
        ],
        2028,
        now=now,
    )
    assert not reconciler._has_stable_shrink_confirmation(
        [
            ("applied", 2030, problem, now - timedelta(hours=6)),
            ("rejected", 2029, problem, now - timedelta(hours=12)),
        ],
        2028,
        now=now,
    )
