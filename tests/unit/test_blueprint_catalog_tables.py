"""Il mapping blueprint → stampa deve coprire tutte le tabelle del catalogo.

Una tabella dimenticata qui non produce un errore: le inserzioni di quel gioco
restano semplicemente non mappabili e spariscono dall'inventario senza traccia.
I test leggono la sola sorgente statica (nessun MySQL, nessun Redis).
"""

from __future__ import annotations

import ast
from pathlib import Path

MAPPER_SOURCE = (
    Path(__file__).resolve().parents[2] / "app" / "services" / "blueprint_mapper.py"
)


def _catalog_print_tables() -> tuple[str, ...]:
    """Legge CATALOG_PRINT_TABLES senza importare Redis/MySQL."""
    module = ast.parse(MAPPER_SOURCE.read_text())
    for node in module.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "CATALOG_PRINT_TABLES":
            return tuple(ast.literal_eval(node.value))
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "CATALOG_PRINT_TABLES" for target in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("CATALOG_PRINT_TABLES non trovata in blueprint_mapper.py")


def test_every_supported_game_has_a_catalog_table() -> None:
    tables = _catalog_print_tables()

    assert tables == (
        "cards_prints",      # Magic
        "op_prints",         # One Piece
        "pk_prints",         # Pokémon
        "lorcana_prints",    # Disney Lorcana
        "sealed_products",   # sigillati di tutti i giochi
    )


def test_table_names_are_safe_to_interpolate_in_sql() -> None:
    # I nomi entrano nella query per interpolazione (un identificatore non può
    # essere un segnaposto): restano una costante del codice, mai input utente.
    for table in _catalog_print_tables():
        assert table.replace("_", "").isalnum(), table
        assert table.islower(), table


def test_queries_are_generated_from_the_single_registry() -> None:
    source = MAPPER_SOURCE.read_text()

    # Nessuna lista di tabelle duplicata: aggiungere un gioco deve restare
    # una riga sola in CATALOG_PRINT_TABLES.
    assert source.count('"lorcana_prints"') == 1
    assert "for table_name in CATALOG_PRINT_TABLES" in source
    assert "params = uncached_ids * len(CATALOG_PRINT_TABLES)" in source
