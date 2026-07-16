import pytest

from app.services.webhook_ledger_processor import classify_order_action


@pytest.mark.parametrize(
    ("cause", "state", "via_zero", "expected"),
    [
        ("order.create", "paid", False, "decrement"),
        ("order.update", "hub_pending", True, "decrement"),
        ("order.update", "paid", True, "ignore"),
        ("order.update", "request_for_cancel", False, "ignore"),
        ("order.update", "canceled", False, "restore"),
        ("order.destroy", "", False, "restore"),
    ],
)
def test_cardtrader_order_action(cause, state, via_zero, expected):
    assert classify_order_action(cause, state, via_zero) == expected
