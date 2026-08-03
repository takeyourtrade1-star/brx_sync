from __future__ import annotations

from queue import Empty
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core import database


class _ExhaustedQueue:
    def get_nowait(self):
        raise Empty

    def get(self, timeout):
        assert timeout == 5
        raise Empty


def test_mysql_pool_exhaustion_never_opens_an_untracked_fallback_connection():
    previous_pool = database._mysql_pool
    previous_count = database._mysql_open_connections
    database._mysql_pool = _ExhaustedQueue()
    database._mysql_open_connections = 1
    create = Mock()
    try:
        with (
            patch.object(
                database,
                "settings",
                SimpleNamespace(MYSQL_POOL_SIZE=1, MYSQL_POOL_MAX_OVERFLOW=0),
            ),
            patch.object(database, "_create_mysql_connection", create),
        ):
            with pytest.raises(TimeoutError, match="pool exhausted"):
                database.get_mysql_connection()
        create.assert_not_called()
        assert database._mysql_open_connections == 1
    finally:
        database._mysql_pool = previous_pool
        database._mysql_open_connections = previous_count
