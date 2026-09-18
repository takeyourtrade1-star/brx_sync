"""Explicit local catalog choices when disconnecting CardTrader."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import dialect
from app.api.v1.routes.sync import disconnect_sync, get_inventory
from app.api.v1.schemas import DisconnectSyncRequest

OWNER = uuid.UUID('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')


def result(value):
    return Mock(scalar_one_or_none=Mock(return_value=value))


def setup(monkeypatch, mode='real', pending=None, unresolved=None):
    settings = SimpleNamespace(execution_mode=mode, sync_status='active')
    session = AsyncMock()
    results = [result(settings), result(unresolved), result(None), result(pending)]
    session.execute.side_effect = results
    connection = AsyncMock()
    session.connection.return_value = connection
    encrypt = Mock(return_value='encrypted-empty')
    monkeypatch.setattr('app.core.crypto.get_encryption_manager', lambda: SimpleNamespace(encrypt=encrypt))
    return session, connection, encrypt, results


def sql(statement):
    return str(statement.compile(dialect=dialect(), compile_kwargs={'literal_binds': True}))


async def disconnect(session, choice=None, action='remove'):
    return await disconnect_sync(str(OWNER), DisconnectSyncRequest(action=action, inventory_action=choice), str(OWNER), session)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['real', 'partial'])
async def test_keep_preserves_namespace_and_revokes_credentials(monkeypatch, mode):
    session, connection, encrypt, _ = setup(monkeypatch, mode=mode)
    response = await disconnect(session, 'keep')
    assert response['execution_mode'] == mode
    assert response['inventory_action'] == 'keep'
    assert response['removed_items'] == 0
    encrypt.assert_called_once_with('')
    statement, parameters = connection.execute.call_args.args
    assert parameters['inventory_mode'] == mode
    assert parameters['user_id'] == str(OWNER)
    assert parameters['token'] == 'encrypted-empty'
    assert 'webhook_secret = NULL' in str(statement)
    assert 'writes_enabled = FALSE' in str(statement)
    assert 'mode_version = mode_version + 1' in str(statement)
    assert all(not sql(call.args[0]).startswith('UPDATE user_inventory_items') for call in session.execute.call_args_list)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_keep_after_suspend_recovers_latest_namespace(monkeypatch):
    session, connection, _, results = setup(monkeypatch, mode='demo')
    session.execute.side_effect = [*results, result('partial')]
    assert (await disconnect(session, 'keep'))['execution_mode'] == 'partial'
    selection = sql(session.execute.call_args.args[0])
    assert "user_inventory_items.source = 'cardtrader'" in selection
    assert str(OWNER) in selection
    assert 'ORDER BY' in selection


@pytest.mark.asyncio
async def test_delete_archives_only_owner_cardtrader_rows_with_unlink(monkeypatch):
    session, connection, _, results = setup(monkeypatch)
    session.execute.side_effect = [*results, Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[0, 0, 0])))), Mock(rowcount=3)]
    response = await disconnect(session, 'delete')
    statement = sql(session.execute.call_args.args[0])
    assert statement.startswith('UPDATE user_inventory_items SET')
    assert "lifecycle_status='archived'" in statement
    assert 'quantity=0' in statement
    assert 'row_version=(user_inventory_items.row_version + 1)' in statement
    assert "user_inventory_items.source = 'cardtrader'" in statement
    assert str(OWNER) in statement
    assert response['removed_items'] == 3
    assert connection.execute.call_args.args[1]['inventory_mode'] == 'demo'
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_reserved_cards_prevent_delete_and_credential_removal(monkeypatch):
    session, connection, encrypt, results = setup(monkeypatch)
    session.execute.side_effect = [*results, Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[0, 2]))))]
    with pytest.raises(HTTPException) as exc:
        await disconnect(session, 'delete')
    assert exc.value.status_code == 409
    session.commit.assert_not_awaited()
    connection.execute.assert_not_awaited()
    encrypt.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('choice', ['keep', 'delete'])
@pytest.mark.parametrize('blocking', ['pending', 'unresolved'])
async def test_active_operations_block_disconnect(monkeypatch, choice, blocking):
    session, connection, _, _ = setup(monkeypatch, **{blocking: 42})
    with pytest.raises(HTTPException) as exc:
        await disconnect(session, choice)
    assert exc.value.status_code == 409
    connection.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_remove_stays_non_destructive(monkeypatch):
    session, connection, _, _ = setup(monkeypatch)
    response = await disconnect(session)
    assert response['removed_items'] == 0
    assert response['inventory_action'] is None
    assert connection.execute.call_args.args[1]['inventory_mode'] == 'demo'


@pytest.mark.asyncio
async def test_suspension_keeps_credentials_and_rejects_catalog_choice(monkeypatch):
    session, connection, encrypt, _ = setup(monkeypatch)
    await disconnect(session, action='suspend')
    encrypt.assert_not_called()
    assert 'cardtrader_token_encrypted' not in str(connection.execute.call_args.args[0])
    session, connection, _, _ = setup(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await disconnect(session, 'delete', 'suspend')
    assert exc.value.status_code == 422
    connection.execute.assert_not_awaited()


def test_invalid_choice_rejected():
    with pytest.raises(ValidationError):
        DisconnectSyncRequest(action='remove', inventory_action='erase_everything')


@pytest.mark.asyncio
async def test_saved_inventory_readable_in_retained_namespace(monkeypatch):
    monkeypatch.setattr('app.api.v1.routes.sync._load_inventory_metrics', AsyncMock(return_value={}))
    session = AsyncMock()
    session.execute.side_effect = [result('real'), Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[])))), Mock(scalar_one=Mock(return_value=0))]
    await get_inventory(str(OWNER), limit=100, offset=0, include_history=False, include_anomalies=False, verified_user_id=str(OWNER), session=session)
    statement = sql(session.execute.call_args_list[1].args[0])
    assert "user_inventory_items.environment = 'real'" in statement
    assert "user_inventory_items.source = 'cardtrader'" in statement
    assert "NOT IN ('archived', 'pending_delete')" in statement
