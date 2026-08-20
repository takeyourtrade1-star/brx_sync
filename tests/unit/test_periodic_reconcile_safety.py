"""Concurrency regression tests for periodic reconciliation."""

import uuid
from types import SimpleNamespace

import pytest

from app.tasks import periodic_sync


class OwnershipRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.eval_calls: list[tuple[str, str]] = []

    def set(self, key: str, value: str, **_kwargs) -> bool:
        if key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, _script: str, _keys: int, key: str, owner: str) -> int:
        self.eval_calls.append((key, owner))
        if self.values.get(key) != owner:
            return 0
        del self.values[key]
        return 1


@pytest.mark.asyncio
async def test_expired_worker_cannot_delete_successor_lock(monkeypatch) -> None:
    user_id = uuid.uuid4()
    settings = SimpleNamespace(user_id=user_id)
    redis = OwnershipRedis()

    async def no_deferred(_user_id):
        return {"processed": 0}

    async def replace_owner(_session, _settings, _mapper):
        key = periodic_sync.LOCK_KEY.format(user_id=user_id)
        redis.values[key] = "successor"
        return {"user_id": str(user_id), "status": "ok"}

    monkeypatch.setattr(periodic_sync, "process_deferred_webhooks", no_deferred)
    monkeypatch.setattr(periodic_sync, "reconcile_user_apply", replace_owner)

    result = await periodic_sync._reconcile_one(None, settings, redis, None)
    key = periodic_sync.LOCK_KEY.format(user_id=user_id)

    assert result["status"] == "ok"
    assert redis.values[key] == "successor"
    assert len(redis.eval_calls) == 1


@pytest.mark.asyncio
async def test_deferred_webhooks_are_processed_before_export(monkeypatch) -> None:
    user_id = uuid.uuid4()
    settings = SimpleNamespace(user_id=user_id)
    redis = OwnershipRedis()
    calls: list[str] = []

    async def process_first(_user_id):
        calls.append("webhooks")
        return {"processed": 1}

    async def export_second(_session, _settings, _mapper):
        calls.append("snapshot")
        return {"user_id": str(user_id), "status": "ok"}

    monkeypatch.setattr(periodic_sync, "process_deferred_webhooks", process_first)
    monkeypatch.setattr(periodic_sync, "reconcile_user_apply", export_second)

    await periodic_sync._reconcile_one(None, settings, redis, None)

    assert calls == ["webhooks", "snapshot"]


def test_periodic_task_fans_out_one_task_per_user(monkeypatch) -> None:
    user_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    task_specs = [(user_id, f"task-{index}") for index, user_id in enumerate(user_ids, start=1)]
    queued: list[str] = []

    def return_user_ids(coroutine):
        coroutine.close()
        return task_specs

    def enqueue(*, args, task_id):
        queued.append(args[0])
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr(periodic_sync, "run_async", return_user_ids)
    monkeypatch.setattr(periodic_sync.reconcile_user, "apply_async", enqueue)

    result = periodic_sync.reconcile_all_users.run()

    assert queued == user_ids
    assert result == {
        "users": 2,
        "queued": 2,
        "dispatch_failures": 0,
        "task_ids": ["task-1", "task-2"],
    }


@pytest.mark.parametrize(
    ("outcome", "operation_status", "failure_code"),
    [
        ("rejected", "failed", "snapshot_rejected"),
        ("deferred", "cancelled", "reconcile_deferred"),
        ("superseded", "cancelled", "reconcile_superseded"),
        ("locked", "cancelled", "reconcile_locked"),
        ("skipped", "cancelled", "reconcile_skipped"),
    ],
)
def test_non_success_domain_outcomes_cannot_be_reported_completed(
    outcome: str,
    operation_status: str,
    failure_code: str,
) -> None:
    status, metadata = periodic_sync._terminal_reconcile_operation(
        {"status": outcome, "user_id": str(uuid.uuid4())}
    )

    assert status == operation_status
    assert metadata["failure_code"] == failure_code


def test_only_ok_is_a_successful_reconcile_operation() -> None:
    result = {"status": "ok", "applied": {"updated": 3}}

    status, metadata = periodic_sync._terminal_reconcile_operation(result)

    assert status == "completed"
    assert metadata == {"result": result}
