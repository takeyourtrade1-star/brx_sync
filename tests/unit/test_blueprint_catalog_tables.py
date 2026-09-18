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
    """Nessun secondo elenco di tabelle, qualunque forma prenda il codice.

    Le assunzioni sono sui nomi, non sulla struttura: il mapper è già stato
    riscritto una volta (da una query per tabella a una UNION ALL sola) e un
    test che citava le righe di allora sarebbe fallito pur essendo il codice
    corretto. Quel che deve restare vero è che aggiungere un gioco sia una riga
    in CATALOG_PRINT_TABLES e nient'altro.
    """
    source = MAPPER_SOURCE.read_text()

    for table in _catalog_print_tables():
        assert source.count(f'"{table}"') == 1, (
            f"{table} compare più di una volta: c'è un secondo elenco da aggiornare a mano"
        )

    # E il registro deve essere davvero usato, non solo dichiarato.
    usi = source.count("CATALOG_PRINT_TABLES") - 1  # meno la definizione
    assert usi >= 2, "CATALOG_PRINT_TABLES dichiarata ma quasi non usata"
