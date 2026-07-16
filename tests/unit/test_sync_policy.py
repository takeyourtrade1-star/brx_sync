import uuid
from types import SimpleNamespace

import pytest

from app.services.sync_policy import (
    CardTraderWriteBlockedError,
    SyncExecutionMode,
    policy_from_settings,
)


def _settings(**overrides):
    values = {
        "user_id": uuid.uuid4(),
        "execution_mode": "demo",
        "mode_version": 1,
        "writes_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("mode", ["demo", "partial", "real"])
def test_policy_parses_supported_modes(mode):
    policy = policy_from_settings(_settings(execution_mode=mode))
    assert policy.execution_mode is SyncExecutionMode(mode)


def test_policy_rejects_unknown_mode():
    with pytest.raises(CardTraderWriteBlockedError):
        policy_from_settings(_settings(execution_mode="legacy"))


def test_policy_keeps_kill_switch_fail_closed():
    policy = policy_from_settings(_settings(execution_mode="real", writes_enabled=False))
    assert policy.writes_enabled is False
