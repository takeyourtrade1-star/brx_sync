import uuid

import pytest

from app.api.internal_schemas import ReservationItemRequest, ReserveInventoryRequest
from app.core.config import get_settings
from app.models.inventory import UserInventoryItem
from app.services.inventory_operations import InventoryOperationError, reserve_inventory
from tests.integration.test_trade_inventory_operations import (
    FakeCardTraderClient, add_inventory_item, client_factory, real_sync_settings,
)


@pytest.mark.asyncio
async def test_missing_catalog_preserves_owned_stock_and_blocks_reservation(
    test_session_factory, monkeypatch,
):
    monkeypatch.setattr(get_settings(), 'CARDTRADER_WRITES_ENABLED', True)
    user_id = uuid.uuid4()
    async with test_session_factory() as session:
        async with session.begin():
            session.add(real_sync_settings(user_id))
            item = await add_inventory_item(
                session, user_id=user_id, quantity=3, source='cardtrader',
                external_stock_id='427629843', blueprint_id=393523,
            )
            item.mapping_status = 'missing'
            item_id = item.id

    client = FakeCardTraderClient({427629843: 3})
    async with test_session_factory() as session:
        with pytest.raises(InventoryOperationError) as blocked:
            await reserve_inventory(
                session,
                ReserveInventoryRequest(
                    op_key='catalog-pending-reservation', user_id=user_id,
                    items=[ReservationItemRequest(item_id=item_id, quantity=1)],
                ),
                client_factory=client_factory(client), decrypt_token=lambda token: token,
            )
    assert blocked.value.code == 'INVENTORY_UNAVAILABLE'
    assert client.calls == []
    async with test_session_factory() as session:
        item = await session.get(UserInventoryItem, item_id)
        assert (item.quantity, item.reserved_quantity, item.mapping_status) == (3, 0, 'missing')
