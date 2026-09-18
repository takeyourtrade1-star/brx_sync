from contextlib import contextmanager
from unittest.mock import MagicMock

from app.services.blueprint_mapper import BlueprintMapper


def mapper_with_rows(monkeypatch, rows):
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value = cursor

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setattr('app.core.database.get_mysql_connection_context', connection)
    mapper = BlueprintMapper.__new__(BlueprintMapper)
    mapper.redis = MagicMock()
    mapper.redis.get.return_value = None
    return mapper


def test_single_and_batch_reject_cross_table_identity_collision(monkeypatch):
    mapper = mapper_with_rows(monkeypatch, [
        {'id': 10, 'table_name': 'cards_prints', 'cardtrader_id': 42},
        {'id': 20, 'table_name': 'pk_prints', 'cardtrader_id': 42},
    ])
    assert mapper.map_blueprint_id(42) is None
    assert mapper.batch_map_blueprint_ids([42]) == {42: None}
    mapper.redis.setex.assert_not_called()


def test_mapping_preserves_mysql_identity_distinct_from_cardtrader(monkeypatch):
    mapper = mapper_with_rows(monkeypatch, [
        {'id': 64048, 'table_name': 'cards_prints', 'cardtrader_id': 40085},
    ])
    assert mapper.batch_map_blueprint_ids([40085, 393523]) == {
        40085: (64048, 'cards_prints'), 393523: None,
    }
    mapper.redis.setex.assert_called_once_with(
        'blueprint_mapping:v2:40085', 86400, '64048:cards_prints'
    )


def test_unknown_cached_table_is_not_authority(monkeypatch):
    mapper = mapper_with_rows(monkeypatch, [])
    mapper.redis.get.return_value = '10:unknown'
    assert mapper.map_blueprint_id(42) is None
