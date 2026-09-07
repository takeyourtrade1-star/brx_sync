"""Read-only CardTrader catalog import and canonical MySQL write boundary.

The importer joins identities only through exact provider IDs and the exact
Scryfall UUID exposed by CardTrader.  It never performs a name match and never
calls a CardTrader mutating endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

import httpx
import pymysql

from app.core.config import get_settings

logger = logging.getLogger(__name__)

MAGIC_GAME_ID = 1
MAGIC_CATEGORY_ID = 1
_MAX_BLUEPRINT_ID = 2**63 - 1
_MAX_EXPANSIONS_SCAN = 512
_CACHE_MAX_ENTRIES = 32
_SHARED_BLUEPRINT_CACHE: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
_SHARED_EXPANSIONS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _cache_lookup(
    cache: dict[Any, tuple[float, list[dict[str, Any]]]],
    key: Any,
    now: float,
) -> list[dict[str, Any]] | None:
    """Read a bounded cache and opportunistically remove expired entries."""

    for cached_key, (expires_at, _value) in list(cache.items()):
        if expires_at <= now:
            cache.pop(cached_key, None)
    cached = cache.get(key)
    return list(cached[1]) if cached is not None else None


def _cache_store(
    cache: dict[Any, tuple[float, list[dict[str, Any]]]],
    key: Any,
    value: list[dict[str, Any]],
    expires_at: float,
    now: float,
) -> None:
    """Store one cache value while keeping process-wide memory bounded."""

    for cached_key, (cached_expires_at, _cached_value) in list(cache.items()):
        if cached_expires_at <= now:
            cache.pop(cached_key, None)
    cache.pop(key, None)
    if len(cache) >= _CACHE_MAX_ENTRIES:
        oldest_key = min(cache, key=lambda cached_key: cache[cached_key][0])
        cache.pop(oldest_key, None)
    cache[key] = (expires_at, value)


class CatalogImportError(Exception):
    """Base error with a durable classification for the worker."""

    retryable = True

    def __init__(self, code: str, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.code = code
        if retryable is not None:
            self.retryable = retryable


class CatalogImportNeedsReview(CatalogImportError):
    """The provider response is incomplete or ambiguous and must be retained."""

    retryable = False


class CatalogImportTransientError(CatalogImportError):
    """A bounded retry may succeed without changing catalog identity."""

    retryable = True


@dataclass(frozen=True)
class CanonicalCatalogRecord:
    """All values needed to upsert one canonical card, set and print."""

    oracle_id: str
    name: str
    cmc: float
    color_identity: list[str]
    colors: list[str]
    keywords: list[str]
    type_line: str
    legalities: dict[str, str]
    set_cardtrader_id: int
    set_code: str | None
    set_name: str
    release_date: date | None
    cardtrader_id: int
    scryfall_id: str
    collector_number: str | None
    rarity: str | None
    image_path: str | None
    available_languages: list[str]
    has_foil: bool
    has_signed: bool
    has_altered: bool
    condition_options: list[str]
    # Live evidence shows newly inserted prints using the sentinel 0.  There
    # is no base-card table in the shared schema, so do not infer another ID.
    base_card_id: int = 0


@dataclass(frozen=True)
class CatalogPrintReference:
    """The local identity assigned by MySQL, which Search must preserve."""

    local_print_id: int
    cardtrader_id: int


@dataclass(frozen=True)
class CatalogImportResult:
    """Successful import result with the stable Search document."""

    blueprint_id: int
    scryfall_id: str
    local_print_id: int
    document: dict[str, Any]


class CatalogBlueprintReader(Protocol):
    """Exact CardTrader blueprint reader. Implementations must issue GET only."""

    async def fetch_exact_blueprint(
        self, blueprint_id: int, expansion_id: int | None = None
    ) -> Mapping[str, Any]: ...


class ScryfallCardReader(Protocol):
    """Exact Scryfall UUID reader."""

    async def fetch_exact_card(self, scryfall_id: str) -> Mapping[str, Any]: ...


class CatalogMysqlWriter(Protocol):
    """Dedicated worker boundary for canonical MySQL DML."""

    async def upsert_canonical(
        self, record: CanonicalCatalogRecord
    ) -> CatalogPrintReference: ...


class CatalogIndexPublisher(Protocol):
    """Search publisher called only after the MySQL transaction commits."""

    async def publish(self, document: Mapping[str, Any]) -> None: ...


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CatalogImportNeedsReview("invalid_" + field, f"{field} is not an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise CatalogImportNeedsReview("invalid_" + field, f"{field} is not an integer") from exc
    if result <= 0 or result > _MAX_BLUEPRINT_ID:
        raise CatalogImportNeedsReview("invalid_" + field, f"{field} is out of range")
    return result


def _unwrap_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        resource = value.get("resource")
        if isinstance(resource, Mapping):
            return resource
        return value
    raise CatalogImportNeedsReview("invalid_provider_payload", "provider payload is not an object")


def _list_payload(value: Any, keys: tuple[str, ...]) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        candidates = value
    elif isinstance(value, Mapping):
        candidates = []
        for key in keys:
            nested = value.get(key)
            if isinstance(nested, list):
                candidates = nested
                break
    else:
        candidates = []
    return [item for item in candidates if isinstance(item, Mapping)]


def _expansion_id_from_product(product: Mapping[str, Any]) -> int | None:
    value = product.get("expansion_id")
    if value is None:
        expansion = product.get("expansion")
        if isinstance(expansion, Mapping):
            value = expansion.get("id")
    if value is None and isinstance(product.get("catalog_metadata"), Mapping):
        metadata = product["catalog_metadata"]
        value = metadata.get("expansion_id")
        if value is None and isinstance(metadata.get("expansion"), Mapping):
            value = metadata["expansion"].get("id")
    if value is None:
        return None
    return _positive_int(value, "expansion_id")


def _blueprint_image_path(blueprint: Mapping[str, Any]) -> str | None:
    value = blueprint.get("image_url")
    image = blueprint.get("image")
    if value is None and isinstance(image, Mapping):
        value = image.get("url")
        if value is None and isinstance(image.get("show"), Mapping):
            value = image["show"].get("url")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:255] if value else None


def _string_list(value: Any, *, field: str, required: bool = False) -> list[str]:
    if value is None:
        if required:
            raise CatalogImportNeedsReview("missing_" + field, f"{field} is missing")
        return []
    if not isinstance(value, list):
        raise CatalogImportNeedsReview("invalid_" + field, f"{field} is not a list")
    result = [str(item).strip() for item in value if isinstance(item, str) and item.strip()]
    if required and not result:
        raise CatalogImportNeedsReview("missing_" + field, f"{field} is empty")
    return result[:64]


def _legalities(value: Any) -> dict[str, str]:
    if value is None:
        raise CatalogImportNeedsReview("missing_legalities", "Scryfall legalities are missing")
    if not isinstance(value, Mapping):
        raise CatalogImportNeedsReview("invalid_legalities", "Scryfall legalities are not an object")
    return {
        str(key): str(item)
        for key, item in list(value.items())[:64]
        if isinstance(key, str) and isinstance(item, str)
    }


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise CatalogImportNeedsReview("invalid_" + field, f"{field} is not numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CatalogImportNeedsReview("invalid_" + field, f"{field} is not numeric") from exc
    if not math.isfinite(number) or number < 0 or number > 1000:
        raise CatalogImportNeedsReview("invalid_" + field, f"{field} is out of range")
    return number


def _uuid_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise CatalogImportNeedsReview("missing_" + field, f"{field} is missing")
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError) as exc:
        raise CatalogImportNeedsReview("invalid_" + field, f"{field} is not a UUID") from exc


def _date(value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogImportNeedsReview("invalid_release_date", "release date is not a string")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise CatalogImportNeedsReview("invalid_release_date", "release date is invalid") from exc


def _canonical_rarity(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().casefold()
    known = {
        "common": "Common",
        "uncommon": "Uncommon",
        "rare": "Rare",
        "mythic": "Mythic",
        "mythic rare": "Mythic",
    }
    return known.get(normalized, value.strip()[:20].capitalize())


def _exact_record(
    blueprint: Mapping[str, Any],
    scryfall: Mapping[str, Any],
    *,
    expected_blueprint_id: int,
    expected_expansion_id: int | None,
) -> CanonicalCatalogRecord:
    blueprint_id = _positive_int(blueprint.get("id"), "blueprint_id")
    if blueprint_id != expected_blueprint_id:
        raise CatalogImportNeedsReview(
            "blueprint_id_mismatch",
            f"CardTrader returned blueprint {blueprint_id}, expected {expected_blueprint_id}",
        )
    if blueprint.get("game_id") != MAGIC_GAME_ID:
        raise CatalogImportNeedsReview("unsupported_game", "blueprint is not Magic game_id=1")
    if blueprint.get("category_id") != MAGIC_CATEGORY_ID:
        raise CatalogImportNeedsReview(
            "unsupported_category", "blueprint is not a single card category_id=1"
        )

    expansion_id = blueprint.get("expansion_id")
    expansion = blueprint.get("expansion")
    if expansion_id is None and isinstance(expansion, Mapping):
        expansion_id = expansion.get("id")
    expansion_id = _positive_int(expansion_id, "expansion_id")
    if expected_expansion_id is not None and expansion_id != expected_expansion_id:
        raise CatalogImportNeedsReview(
            "expansion_id_mismatch",
            f"blueprint expansion {expansion_id} differs from product expansion {expected_expansion_id}",
        )

    scryfall_id = _uuid_string(blueprint.get("scryfall_id"), "scryfall_id")
    provider_scryfall_id = _uuid_string(scryfall.get("id"), "scryfall_id_response")
    if provider_scryfall_id != scryfall_id:
        raise CatalogImportNeedsReview(
            "scryfall_id_mismatch", "Scryfall response does not match CardTrader blueprint"
        )
    oracle_id = _uuid_string(scryfall.get("oracle_id"), "oracle_id")
    name = scryfall.get("name")
    type_line = scryfall.get("type_line")
    expansion_name = expansion.get("name") if isinstance(expansion, Mapping) else None
    scryfall_set_name = scryfall.get("set_name")
    set_name = expansion_name if isinstance(expansion_name, str) and expansion_name.strip() else scryfall_set_name
    if not all(isinstance(value, str) and value.strip() for value in (name, type_line, set_name)):
        raise CatalogImportNeedsReview("missing_scryfall_metadata", "required Scryfall text is missing")

    set_code_value = expansion.get("code") if isinstance(expansion, Mapping) else None
    if not isinstance(set_code_value, str) or not set_code_value.strip():
        set_code_value = scryfall.get("set")
    set_code = str(set_code_value).strip()[:20] if isinstance(set_code_value, str) else None

    fixed = blueprint.get("fixed_properties")
    fixed_properties = fixed if isinstance(fixed, Mapping) else {}
    editable = blueprint.get("editable_properties")
    condition_options: list[str] = []
    editable_languages: list[str] = []
    foil_allowed = False
    signed_allowed = False
    altered_allowed = False
    if isinstance(editable, list):
        for item in editable:
            if not isinstance(item, Mapping):
                continue
            property_name = item.get("name")
            possible_values = item.get("possible_values")
            if property_name == "condition":
                condition_options = _string_list(possible_values, field="condition_options")
            elif property_name == "mtg_language":
                editable_languages = _string_list(possible_values, field="languages")
            elif property_name in {"mtg_foil", "foil", "etched"}:
                foil_allowed = foil_allowed or isinstance(possible_values, list) and True in possible_values
            elif property_name == "signed":
                signed_allowed = signed_allowed or isinstance(possible_values, list) and True in possible_values
            elif property_name == "altered":
                altered_allowed = altered_allowed or isinstance(possible_values, list) and True in possible_values

    lang = scryfall.get("lang")
    languages = editable_languages or (
        [str(lang).strip()] if isinstance(lang, str) and lang.strip() else ["en"]
    )
    finishes = _string_list(scryfall.get("finishes"), field="finishes")
    image_uris = scryfall.get("image_uris")
    image_path = _blueprint_image_path(blueprint)
    if image_path is None and isinstance(image_uris, Mapping):
        normal = image_uris.get("normal") or image_uris.get("large")
        image_path = str(normal).strip()[:255] if isinstance(normal, str) and normal.strip() else None

    collector = fixed_properties.get("collector_number")
    if collector is None:
        collector = scryfall.get("collector_number")
    if collector is None and isinstance(fixed_properties.get("collector_number"), str):
        collector = fixed_properties["collector_number"]
    rarity = _canonical_rarity(fixed_properties.get("mtg_rarity") or scryfall.get("rarity"))
    return CanonicalCatalogRecord(
        oracle_id=oracle_id,
        name=name.strip()[:255],
        cmc=_finite_number(scryfall.get("cmc"), "cmc"),
        color_identity=_string_list(scryfall.get("color_identity"), field="color_identity"),
        colors=_string_list(scryfall.get("colors"), field="colors"),
        keywords=_string_list(scryfall.get("keywords"), field="keywords"),
        type_line=type_line.strip()[:255],
        legalities=_legalities(scryfall.get("legalities")),
        set_cardtrader_id=expansion_id,
        set_code=set_code,
        set_name=set_name.strip()[:255],
        release_date=_date(scryfall.get("released_at")),
        cardtrader_id=blueprint_id,
        scryfall_id=scryfall_id,
        collector_number=str(collector).strip()[:20] if collector is not None else None,
        rarity=str(rarity).strip()[:20] if rarity is not None else None,
        image_path=image_path,
        available_languages=languages[:16],
        has_foil=foil_allowed or "foil" in finishes or "etched" in finishes,
        has_signed=signed_allowed,
        has_altered=altered_allowed,
        condition_options=condition_options,
    )


def _search_tokens(name: str) -> list[str]:
    """Small local equivalent of Search's prefix tokens for targeted writes."""

    words = [word.casefold() for word in name.split() if word]
    tokens: set[str] = set(words)
    for word in words:
        tokens.update(word[:index] for index in range(2, len(word) + 1))
    if len(words) > 1:
        tokens.add("".join(word[0] for word in words))
    return sorted(tokens)[:256]


def build_search_document(
    record: CanonicalCatalogRecord,
    reference: CatalogPrintReference,
) -> dict[str, Any]:
    """Build the Search shape with the local ``cards_prints.id`` identity."""

    return {
        "id": f"mtg_{reference.local_print_id}",
        "name": record.name,
        "set_name": record.set_name,
        "set_code": record.set_code or "",
        "release_date": record.release_date.isoformat() if record.release_date else None,
        "set_icon_uri": None,
        "game_slug": "mtg",
        "category_id": MAGIC_CATEGORY_ID,
        "category_name": "Carta Singola",
        "image": record.image_path or "",
        "cardtrader_id": record.cardtrader_id,
        "oracle_id": record.oracle_id,
        "collector_number": record.collector_number,
        "rarity": record.rarity,
        "available_languages": record.available_languages,
        "foil_available": record.has_foil,
        "keywords_localized": [record.name],
        "search_tokens": _search_tokens(record.name),
    }


class CatalogImporter:
    """Compose exact CT/Scryfall reads, canonical DML and Search projection."""

    def __init__(
        self,
        blueprint_reader: CatalogBlueprintReader,
        scryfall_reader: ScryfallCardReader,
        mysql_writer: CatalogMysqlWriter,
    ) -> None:
        self.blueprint_reader = blueprint_reader
        self.scryfall_reader = scryfall_reader
        self.mysql_writer = mysql_writer

    async def import_blueprint(
        self,
        blueprint_id: int,
        product: Mapping[str, Any] | None = None,
    ) -> CatalogImportResult:
        expected_blueprint_id = _positive_int(blueprint_id, "blueprint_id")
        product = product or {}
        expected_expansion_id = _expansion_id_from_product(product)
        blueprint = _unwrap_mapping(
            await self.blueprint_reader.fetch_exact_blueprint(
                expected_blueprint_id, expected_expansion_id
            )
        )
        blueprint_scryfall_id = _uuid_string(blueprint.get("scryfall_id"), "scryfall_id")
        scryfall = _unwrap_mapping(
            await self.scryfall_reader.fetch_exact_card(blueprint_scryfall_id)
        )
        record = _exact_record(
            blueprint,
            scryfall,
            expected_blueprint_id=expected_blueprint_id,
            expected_expansion_id=expected_expansion_id,
        )
        reference = await self.mysql_writer.upsert_canonical(record)
        if reference.cardtrader_id != expected_blueprint_id:
            raise CatalogImportNeedsReview(
                "writer_identity_mismatch", "MySQL writer returned a different CardTrader identity"
            )
        document = build_search_document(record, reference)
        return CatalogImportResult(
            blueprint_id=expected_blueprint_id,
            scryfall_id=record.scryfall_id,
            local_print_id=reference.local_print_id,
            document=document,
        )


class CardTraderCatalogReader:
    """CardTrader export adapter restricted to exact GET endpoints."""

    def __init__(self, client: Any, *, user_cache_key: str | None = None) -> None:
        self.client = client
        self.user_cache_key = user_cache_key or str(getattr(client, "user_id", "shared"))

    def _cache_ttl(self) -> float:
        return float(get_settings().CATALOG_IMPORT_EXPANSION_CACHE_SECONDS)

    async def _owned_expansions(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = _cache_lookup(_SHARED_EXPANSIONS_CACHE, self.user_cache_key, now)
        if cached is not None:
            return cached
        payload = await self.client._make_request("GET", "/expansions/export")
        expansions = [
            dict(item)
            for item in _list_payload(payload, ("resource", "resources", "expansions"))
        ]
        expansions = [
            item
            for item in expansions
            if isinstance(item.get("id"), (int, str)) and str(item.get("id")).isdigit()
        ][:_MAX_EXPANSIONS_SCAN]
        _cache_store(
            _SHARED_EXPANSIONS_CACHE,
            self.user_cache_key,
            expansions,
            now + self._cache_ttl(),
            now,
        )
        return expansions

    async def _blueprints_for_expansion(self, expansion_id: int) -> list[dict[str, Any]]:
        cache_key = (self.user_cache_key, expansion_id)
        now = time.monotonic()
        cached = _cache_lookup(_SHARED_BLUEPRINT_CACHE, cache_key, now)
        if cached is not None:
            return [dict(item) for item in cached]
        payload = await self.client._make_request(
            "GET", "/blueprints/export", params={"expansion_id": expansion_id}
        )
        rows = [
            dict(item)
            for item in _list_payload(payload, ("resource", "resources", "blueprints"))
        ]
        # The blueprint export may expose only expansion_id.  Enrich from the
        # same account's owned-expansion export so the CT expansion name/code
        # remains canonical in ``sets``.
        expansion_details = next(
            (
                item
                for item in await self._owned_expansions()
                if str(item.get("id")) == str(expansion_id)
            ),
            None,
        )
        for row in rows:
            row.setdefault("expansion_id", expansion_id)
            if expansion_details is not None:
                row.setdefault("expansion", dict(expansion_details))
        _cache_store(
            _SHARED_BLUEPRINT_CACHE,
            cache_key,
            rows,
            now + self._cache_ttl(),
            now,
        )
        return rows

    async def fetch_exact_blueprint(
        self, blueprint_id: int, expansion_id: int | None = None
    ) -> Mapping[str, Any]:
        expected = _positive_int(blueprint_id, "blueprint_id")
        if expansion_id is None:
            raise CatalogImportNeedsReview(
                "missing_expansion_id",
                "CardTrader product did not provide the expansion ID needed for a bounded export",
            )
        rows = await self._blueprints_for_expansion(_positive_int(expansion_id, "expansion_id"))
        for row in rows:
            try:
                row_id = _positive_int(row.get("id"), "blueprint_id")
            except CatalogImportNeedsReview:
                continue
            if row_id == expected:
                return row
        raise CatalogImportNeedsReview(
            "blueprint_not_found_in_export",
            f"exact CardTrader blueprint {expected} was not found in the bounded expansion export",
        )


class ScryfallHttpReader:
    """Minimal bounded read-only Scryfall adapter."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or get_settings().CATALOG_SCRYFALL_API_BASE_URL).rstrip("/")
        self._throttle_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "EbartexCatalogImporter/1.0",
            },
        )

    async def fetch_exact_card(self, scryfall_id: str) -> Mapping[str, Any]:
        expected = _uuid_string(scryfall_id, "scryfall_id")
        try:
            async with self._throttle_lock:
                elapsed = time.monotonic() - self._last_request_at
                if elapsed < 0.1:
                    await asyncio.sleep(0.1 - elapsed)
                self._last_request_at = time.monotonic()
                response = await self._client.get(f"/cards/{expected}")
        except httpx.HTTPError as exc:
            raise CatalogImportTransientError("scryfall_network", "Scryfall read failed") from exc
        if response.status_code == 429:
            raise CatalogImportTransientError(
                "scryfall_rate_limited", "Scryfall rate limited the catalog worker"
            )
        if response.status_code == 404:
            raise CatalogImportNeedsReview("scryfall_not_found", "Scryfall UUID was not found")
        if response.status_code >= 500:
            raise CatalogImportTransientError(
                "scryfall_server_error", f"Scryfall returned HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise CatalogImportNeedsReview(
                "scryfall_rejected", f"Scryfall returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CatalogImportTransientError("scryfall_invalid_json", "Scryfall JSON is invalid") from exc
        return _unwrap_mapping(payload)

    async def close(self) -> None:
        await self._client.aclose()


ConnectionContextFactory = Callable[[], AbstractContextManager[Any]]


class MySQLCanonicalCatalogWriter:
    """Dedicated least-privilege MySQL writer used only by the catalog worker.

    ``base_card_id=0`` is the live schema's current sentinel for newly inserted
    prints.  It is explicit here so a future schema change cannot silently
    manufacture a relationship from a CardTrader or local print ID.
    """

    def __init__(
        self,
        connection_context_factory: ConnectionContextFactory,
        *,
        base_card_id: int = 0,
    ) -> None:
        if isinstance(base_card_id, bool) or base_card_id < 0:
            raise ValueError("base_card_id must be a non-negative live-schema value")
        self.connection_context_factory = connection_context_factory
        self.base_card_id = base_card_id

    async def upsert_canonical(self, record: CanonicalCatalogRecord) -> CatalogPrintReference:
        return await asyncio.to_thread(self._upsert_sync, record)

    def _upsert_sync(self, record: CanonicalCatalogRecord) -> CatalogPrintReference:
        with self.connection_context_factory() as connection:
            try:
                connection.begin()
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO cards
                            (oracle_id, name, cmc, color_identity, colors, keywords,
                             type_line, legalities)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            name=VALUES(name), cmc=VALUES(cmc),
                            color_identity=VALUES(color_identity), colors=VALUES(colors),
                            keywords=VALUES(keywords), type_line=VALUES(type_line),
                            legalities=VALUES(legalities)
                        """,
                        (
                            record.oracle_id,
                            record.name,
                            record.cmc,
                            json.dumps(record.color_identity),
                            json.dumps(record.colors),
                            json.dumps(record.keywords),
                            record.type_line,
                            json.dumps(record.legalities),
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO sets (cardtrader_id, code, name, release_date, game_id)
                        VALUES (%s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            code=VALUES(code),
                            release_date=VALUES(release_date), game_id=VALUES(game_id)
                        """,
                        (
                            record.set_cardtrader_id,
                            record.set_code,
                            record.set_name,
                            record.release_date,
                            MAGIC_GAME_ID,
                        ),
                    )
                    cursor.execute(
                        "SELECT id FROM sets WHERE cardtrader_id=%s LIMIT 1",
                        (record.set_cardtrader_id,),
                    )
                    set_row = cursor.fetchone()
                    if not isinstance(set_row, Mapping) or not set_row.get("id"):
                        raise CatalogImportNeedsReview("set_upsert_missing", "canonical set id was not returned")
                    set_id = int(set_row["id"])
                    # Lock and validate an existing print before changing any
                    # canonical fields.  The local print ID and a previously
                    # approved image are identities/assets owned by Ebartex.
                    cursor.execute(
                        """
                        SELECT id, cardtrader_id, oracle_id, base_card_id, set_id, scryfall_id,
                               image_path, image_status
                        FROM cards_prints
                        WHERE cardtrader_id=%s OR scryfall_id=%s
                        FOR UPDATE
                        """,
                        (record.cardtrader_id, record.scryfall_id),
                    )
                    existing_prints = cursor.fetchall()
                    if existing_prints:
                        if not all(isinstance(row, Mapping) and row.get("id") for row in existing_prints):
                            raise CatalogImportNeedsReview(
                                "print_identity_unreadable",
                                "existing cards_prints identity could not be read",
                            )
                        if len(existing_prints) != 1:
                            raise CatalogImportNeedsReview(
                                "print_identity_conflict",
                                "CardTrader and Scryfall identities refer to multiple prints",
                            )
                        existing_print = existing_prints[0]
                        existing_cardtrader_id = existing_print.get("cardtrader_id")
                        if (
                            existing_cardtrader_id is not None
                            and int(existing_cardtrader_id) != record.cardtrader_id
                        ):
                            raise CatalogImportNeedsReview(
                                "print_cardtrader_conflict",
                                "existing cards_prints CardTrader identity conflicts with request",
                            )
                        existing_oracle = existing_print.get("oracle_id")
                        if existing_oracle and str(existing_oracle).lower() != record.oracle_id:
                            raise CatalogImportNeedsReview(
                                "print_oracle_conflict",
                                "existing cards_prints oracle identity conflicts with Scryfall",
                            )
                        existing_scryfall = existing_print.get("scryfall_id")
                        if existing_scryfall and str(existing_scryfall).lower() != record.scryfall_id:
                            raise CatalogImportNeedsReview(
                                "print_scryfall_conflict",
                                "existing cards_prints Scryfall identity conflicts with CardTrader",
                            )
                        if existing_print.get("set_id") is not None and int(existing_print["set_id"]) != set_id:
                            raise CatalogImportNeedsReview(
                                "print_set_conflict",
                                "existing cards_prints set identity conflicts with CardTrader",
                            )
                        cursor.execute(
                            """
                            UPDATE cards_prints
                            SET oracle_id=%s, set_id=%s, cardtrader_id=COALESCE(cardtrader_id, %s),
                                scryfall_id=%s,
                                collector_number=%s, rarity=%s,
                                image_path=COALESCE(image_path, %s),
                                available_languages=%s,
                                has_foil=GREATEST(has_foil, %s),
                                has_signed=GREATEST(has_signed, %s),
                                has_altered=GREATEST(has_altered, %s),
                                condition_options=%s
                            WHERE id=%s
                            """,
                            (
                                record.oracle_id,
                                set_id,
                                record.cardtrader_id,
                                record.scryfall_id,
                                record.collector_number,
                                record.rarity,
                                record.image_path,
                                json.dumps(record.available_languages),
                                int(record.has_foil),
                                int(record.has_signed),
                                int(record.has_altered),
                                json.dumps(record.condition_options),
                                int(existing_print["id"]),
                            ),
                        )
                        local_print_id = int(existing_print["id"])
                    else:
                        cursor.execute(
                            """
                            INSERT INTO cards_prints
                                (oracle_id, base_card_id, set_id, cardtrader_id, scryfall_id,
                                 collector_number, rarity, condition_default, image_path,
                                 image_status, available_languages, has_foil, has_signed,
                                 has_altered, condition_options)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                record.oracle_id,
                                self.base_card_id,
                                set_id,
                                record.cardtrader_id,
                                record.scryfall_id,
                                record.collector_number,
                                record.rarity,
                                "NM",
                                record.image_path,
                                "pending",
                                json.dumps(record.available_languages),
                                int(record.has_foil),
                                int(record.has_signed),
                                int(record.has_altered),
                                json.dumps(record.condition_options),
                            ),
                        )
                        local_print_id = int(cursor.lastrowid)
                connection.commit()
                return CatalogPrintReference(local_print_id, record.cardtrader_id)
            except Exception:
                connection.rollback()
                raise


def build_catalog_mysql_writer_from_settings() -> MySQLCanonicalCatalogWriter:
    """Construct the writer only in a worker with explicit writer credentials."""

    settings = get_settings()
    if not settings.CATALOG_MYSQL_WRITE_ENABLED or settings.SERVICE_ROLE != "worker":
        raise CatalogImportNeedsReview(
            "catalog_writer_disabled", "dedicated catalog MySQL writer is disabled"
        )
    if not settings.CATALOG_MYSQL_WRITER_USER or not settings.CATALOG_MYSQL_WRITER_PASSWORD:
        raise CatalogImportNeedsReview(
            "catalog_writer_not_configured", "dedicated catalog MySQL credentials are missing"
        )

    @contextmanager
    def connection_context() -> Any:
        options: dict[str, Any] = {
            "host": settings.MYSQL_HOST,
            "port": settings.MYSQL_PORT,
            "user": settings.CATALOG_MYSQL_WRITER_USER,
            "password": settings.CATALOG_MYSQL_WRITER_PASSWORD.get_secret_value(),
            "database": settings.MYSQL_DATABASE,
            "charset": "utf8mb4",
            "cursorclass": pymysql.cursors.DictCursor,
            "connect_timeout": 5,
            "read_timeout": 20,
            "write_timeout": 20,
            "local_infile": False,
            "autocommit": False,
        }
        if settings.mysql_ssl_context is not None:
            options["ssl"] = settings.mysql_ssl_context
        connection = pymysql.connect(**options)
        try:
            yield connection
        finally:
            connection.close()

    return MySQLCanonicalCatalogWriter(connection_context)
