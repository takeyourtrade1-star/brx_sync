import json

import httpx
import pytest

from app.services.catalog_importer import CatalogImportNeedsReview, CatalogImportTransientError
from app.services.catalog_index_publisher import MeilisearchCatalogPublisher


DOCUMENT = {
    'id': 'mtg_99543', 'cardtrader_id': 393523, 'game_slug': 'mtg',
    'category_id': 1, 'name': 'The Ruinous Wrecking Crew',
}


@pytest.mark.asyncio
async def test_publish_waits_for_ack_and_preserves_legacy_links():
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if request.method == 'GET' and '/documents/' in path:
            return httpx.Response(404)
        if path.endswith('/search'):
            return httpx.Response(200, json={'hits': [{**DOCUMENT, 'id': 'mtg_393523', 'market_price': 1.3}]})
        if path.endswith('/documents'):
            assert json.loads(request.content)[0]['market_price'] == 1.3
            return httpx.Response(202, json={'taskUid': 11})
        assert not path.endswith('/delete-batch')
        return httpx.Response(200, json={'status': 'succeeded'})

    publisher = MeilisearchCatalogPublisher('http://search.test', 'test', transport=httpx.MockTransport(respond))
    await publisher.publish(DOCUMENT)
    assert calls[-1] == ('GET', '/tasks/11')


@pytest.mark.asyncio
async def test_different_blueprint_at_canonical_id_is_never_overwritten():
    def respond(request):
        assert request.method == 'GET'
        return httpx.Response(200, json={**DOCUMENT, 'cardtrader_id': 999})
    publisher = MeilisearchCatalogPublisher('http://search.test', 'test', transport=httpx.MockTransport(respond))
    with pytest.raises(CatalogImportNeedsReview, match='another blueprint'):
        await publisher.publish(DOCUMENT)


@pytest.mark.asyncio
async def test_retry_preserves_existing_translations_and_uses_partial_update():
    existing = {**DOCUMENT, 'keywords_localized': ['The Ruinous Wrecking Crew', 'Squadra devastatrice'], 'search_tokens': ['squadra'], 'min_price': 2.0}

    def respond(request):
        if request.method == 'GET' and '/documents/' in request.url.path:
            return httpx.Response(200, json=existing)
        if request.url.path.endswith('/search'):
            return httpx.Response(200, json={'hits': [existing]})
        if request.url.path.endswith('/documents'):
            assert request.method == 'PUT'
            payload = json.loads(request.content)[0]
            assert 'Squadra devastatrice' in payload['keywords_localized']
            assert payload['search_tokens'] == ['squadra', 'ruinous']
            assert 'min_price' not in payload
            return httpx.Response(202, json={'taskUid': 11})
        return httpx.Response(200, json={'status': 'succeeded'})

    publisher = MeilisearchCatalogPublisher('http://search.test', 'test', transport=httpx.MockTransport(respond))
    await publisher.publish({**DOCUMENT, 'keywords_localized': [DOCUMENT['name']], 'search_tokens': ['ruinous']})


@pytest.mark.asyncio
async def test_failed_index_task_does_not_remove_the_existing_alias():
    def respond(request):
        assert not request.url.path.endswith('/delete-batch')
        if '/documents/' in request.url.path:
            return httpx.Response(404)
        if request.url.path.endswith('/search'):
            return httpx.Response(200, json={'hits': [{**DOCUMENT, 'id': 'mtg_393523'}]})
        if request.url.path.endswith('/documents'):
            return httpx.Response(202, json={'taskUid': 11})
        return httpx.Response(200, json={'status': 'failed'})
    publisher = MeilisearchCatalogPublisher('http://search.test', 'test', transport=httpx.MockTransport(respond))
    with pytest.raises(CatalogImportTransientError, match='failed'):
        await publisher.publish(DOCUMENT)
