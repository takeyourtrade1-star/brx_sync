"""Pure image-source and preservation rules for catalog imports."""

from app.services.catalog_importer import _exact_record, _image_for_existing_print


SCRYFALL_ID = "4d8c8ceb-84cd-46d2-9230-ab6ca4569334"


def _blueprint() -> dict:
    return {
        "id": 393523,
        "game_id": 1,
        "category_id": 1,
        "expansion_id": 4415,
        "expansion": {"id": 4415, "name": "Marvel Super Heroes", "code": "msh"},
        "scryfall_id": SCRYFALL_ID,
        "image_url": "https://cdn.cardtrader.com/provider-only.jpg",
    }


def _scryfall() -> dict:
    return {
        "id": SCRYFALL_ID,
        "oracle_id": "11111111-1111-4111-8111-111111111111",
        "name": "Catalog test card",
        "type_line": "Creature",
        "set_name": "Marvel Super Heroes",
        "set": "msh",
        "cmc": 2,
        "color_identity": [],
        "colors": [],
        "keywords": [],
        "legalities": {},
        "lang": "en",
        "finishes": ["nonfoil"],
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/a/b/card.jpg"},
    }


def test_exact_record_uses_allowlisted_scryfall_image_over_cardtrader_image() -> None:
    record = _exact_record(
        _blueprint(),
        _scryfall(),
        expected_blueprint_id=393523,
        expected_expansion_id=4415,
    )

    assert record.image_path == "https://cards.scryfall.io/normal/front/a/b/card.jpg"


def test_scryfall_image_keeps_provider_cache_version_query() -> None:
    scryfall = _scryfall()
    image = "https://cards.scryfall.io/normal/front/a/b/card.jpg?1750000123"
    scryfall["image_uris"] = {"normal": image}
    record = _exact_record(
        _blueprint(), scryfall,
        expected_blueprint_id=393523, expected_expansion_id=4415,
    )
    assert record.image_path == image
    assert _image_for_existing_print("https://cardtrader.com/old.jpg", "pending", image) == image


def test_exact_record_uses_front_face_for_double_faced_scryfall_card() -> None:
    scryfall = _scryfall()
    scryfall.pop("image_uris")
    scryfall["card_faces"] = [
        {"image_uris": {"normal": "https://c1.scryfall.com/front.jpg"}},
        {"image_uris": {"normal": "https://c2.scryfall.com/back.jpg"}},
    ]

    record = _exact_record(
        _blueprint(),
        scryfall,
        expected_blueprint_id=393523,
        expected_expansion_id=4415,
    )

    assert record.image_path == "https://c1.scryfall.com/front.jpg"


def test_unallowlisted_scryfall_or_cardtrader_image_is_not_public_projection() -> None:
    blueprint = _blueprint()
    scryfall = _scryfall()
    scryfall["image_uris"] = {"normal": "https://images.example.invalid/card.jpg"}

    record = _exact_record(
        blueprint,
        scryfall,
        expected_blueprint_id=393523,
        expected_expansion_id=4415,
    )

    assert record.image_path is None


def test_pending_cardtrader_image_can_be_repaired_with_validated_scryfall_image() -> None:
    old_image = "https://cdn.cardtrader.com/provider-only.jpg"
    new_image = "https://cards.scryfall.io/normal/front/a/b/card.jpg"

    assert _image_for_existing_print(old_image, "pending", new_image) == new_image
    assert _image_for_existing_print(None, "pending", new_image) == new_image
    assert _image_for_existing_print(old_image, "pending", "https://evil.invalid/card.jpg") == old_image


def test_approved_or_cdn_image_is_preserved_during_catalog_retry() -> None:
    approved = "https://cdn.ebartex.com/catalog/mtg_99543.jpg"
    assert _image_for_existing_print(
        approved,
        "ok",
        "https://cards.scryfall.io/normal/front/a/b/new.jpg",
    ) == approved
    assert _image_for_existing_print(
        approved,
        "pending",
        "https://cards.scryfall.io/normal/front/a/b/new.jpg",
    ) == approved
