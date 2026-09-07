"""Exercise canonical writes against the schema observed in production.

Requires a disposable loopback database ending in _test. No application URL fallback.
"""
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymysql
import pytest

from app.services.catalog_importer import (
    CanonicalCatalogRecord, CatalogImportNeedsReview, MySQLCanonicalCatalogWriter,
)


@pytest.fixture
def catalog_connection():
    raw = os.environ.get('CATALOG_TEST_MYSQL_URL')
    if not raw:
        pytest.skip('CATALOG_TEST_MYSQL_URL non configurato')
    url = urlparse(raw)
    database = url.path.lstrip('/')
    assert url.scheme == 'mysql' and url.hostname in {'127.0.0.1', 'localhost'}
    assert database.endswith('_test')

    @contextmanager
    def connection():
        conn = pymysql.connect(
            host=url.hostname, port=url.port or 3306, user=unquote(url.username or ''),
            password=unquote(url.password or ''), database=database,
            cursorclass=pymysql.cursors.DictCursor, autocommit=True,
        )
        try:
            yield conn
        finally:
            conn.close()

    with connection() as conn, conn.cursor() as cur:
        for table in ('cards_prints', 'cards', 'sets'):
            cur.execute(f'DROP TABLE IF EXISTS {table}')
        schema = (Path(__file__).parents[1] / 'fixtures' / 'catalog_schema.sql').read_text()
        for statement in schema.split(';'):
            if statement.strip():
                cur.execute(statement)
    yield connection


@pytest.fixture
def record():
    return CanonicalCatalogRecord(
        oracle_id='b120269d-4118-4bfd-ae33-fb22c477f2df', name='Catalog test print', cmc=3,
        color_identity=['R'], colors=['R'], keywords=[], type_line='Creature', legalities={},
        set_cardtrader_id=4415, set_code='msh', set_name='Marvel Super Heroes', release_date=None,
        cardtrader_id=393523, scryfall_id='4d8c8ceb-84cd-46d2-9230-ab6ca4569334',
        collector_number='224', rarity='Rare', image_path='https://cardtrader.com/test.jpg',
        available_languages=['en', 'it'], has_foil=True, has_signed=False, has_altered=False,
        condition_options=['Near Mint'],
    )


@pytest.mark.asyncio
async def test_writer_is_idempotent_and_uses_mysql_generated_identity(catalog_connection, record):
    writer = MySQLCanonicalCatalogWriter(catalog_connection)
    first = await writer.upsert_canonical(record)
    with catalog_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cards_prints SET base_card_id=%s, image_path=%s, image_status='ok' WHERE id=%s",
            (777, "https://cdn.ebartex.test/approved.jpg", first.local_print_id),
        )
    second = await writer.upsert_canonical(record)
    assert first == second
    assert first.local_print_id == 99543
    assert first.local_print_id != record.cardtrader_id
    with catalog_connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) AS count FROM cards_prints')
        assert cur.fetchone()['count'] == 1
        cur.execute(
            "SELECT id, base_card_id, image_path FROM cards_prints WHERE id=%s",
            (first.local_print_id,),
        )
        preserved = cur.fetchone()
        assert preserved == {
            'id': first.local_print_id,
            'base_card_id': 777,
            'image_path': 'https://cdn.ebartex.test/approved.jpg',
        }


@pytest.mark.asyncio
async def test_conflicting_provider_identity_cannot_replace_a_print(catalog_connection, record):
    writer = MySQLCanonicalCatalogWriter(catalog_connection)
    original = await writer.upsert_canonical(record)
    with pytest.raises(CatalogImportNeedsReview):
        await writer.upsert_canonical(replace(
            record, oracle_id='f87be305-5385-4522-8bff-234749d6d871',
            scryfall_id='cdd4a895-2d7c-4c6b-b5e5-c26212c2a62f',
        ))
    with catalog_connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT oracle_id,scryfall_id FROM cards_prints WHERE id=%s', (original.local_print_id,))
        row = cur.fetchone()
        assert (row['oracle_id'], row['scryfall_id']) == (record.oracle_id, record.scryfall_id)


@pytest.mark.asyncio
async def test_scryfall_identity_cannot_be_reassigned_to_another_cardtrader_print(
    catalog_connection,
    record,
):
    writer = MySQLCanonicalCatalogWriter(catalog_connection)
    await writer.upsert_canonical(record)
    with pytest.raises(CatalogImportNeedsReview, match='CardTrader identity'):
        await writer.upsert_canonical(replace(record, cardtrader_id=393524))
