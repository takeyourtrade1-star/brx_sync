"""
Blueprint ID mapper: maps CardTrader blueprint_id to Ebartex print_id (MySQL).
Uses Redis cache for performance.
"""
import logging
from typing import Optional, Tuple, Dict

from app.core.database import get_mysql_connection
from app.core.redis_client import get_redis_sync

logger = logging.getLogger(__name__)

# Tabelle del catalogo MySQL che espongono `cardtrader_id`, in ordine di priorità.
# Unica fonte di verità: aggiungere un gioco significa aggiungere una riga qui,
# non un nuovo ramo in ciascuna query.
#   cards_prints    → Magic           (game CardTrader 1)
#   op_prints       → One Piece       (game CardTrader 15)
#   pk_prints       → Pokémon         (game CardTrader 5)
#   lorcana_prints  → Disney Lorcana  (game CardTrader 18)
#   sealed_products → prodotti sigillati di tutti i giochi
CATALOG_PRINT_TABLES: tuple[str, ...] = (
    "cards_prints",
    "op_prints",
    "pk_prints",
    "lorcana_prints",
    "sealed_products",
)


class BlueprintMapper:
    """Maps CardTrader blueprint_id to Ebartex print_id and table name."""

    CACHE_TTL = 86400  # 24 hours
    CACHE_PREFIX = "blueprint_mapping:"

    def __init__(self):
        self.redis = get_redis_sync()

    def _get_cache_key(self, blueprint_id: int) -> str:
        """Get Redis cache key for blueprint mapping."""
        return f"{self.CACHE_PREFIX}{blueprint_id}"

    def _get_from_cache(self, blueprint_id: int) -> Optional[Tuple[int, str]]:
        """Get mapping from Redis cache. Returns (print_id, table_name) or None."""
        key = self._get_cache_key(blueprint_id)
        cached = self.redis.get(key)
        
        if cached:
            try:
                # Format: "print_id:table_name"
                parts = cached.split(":", 1)
                # Una voce in cache che nomina una tabella non più supportata
                # (o un id non valido) va ignorata, non propagata.
                if len(parts) == 2 and parts[1] in CATALOG_PRINT_TABLES and int(parts[0]) > 0:
                    return int(parts[0]), parts[1]
            except (ValueError, IndexError):
                logger.warning("Invalid blueprint cache record; ignoring it")
        
        return None

    def _set_cache(self, blueprint_id: int, print_id: int, table_name: str) -> None:
        """Store mapping in Redis cache."""
        key = self._get_cache_key(blueprint_id)
        value = f"{print_id}:{table_name}"
        self.redis.setex(key, self.CACHE_TTL, value)

    def _query_mysql(self, blueprint_id: int) -> Optional[Tuple[int, str]]:
        """
        Query MySQL database for blueprint_id mapping.
        Returns (print_id, table_name) or None if not found.
        """
        from app.core.database import get_mysql_connection_context
        
        with get_mysql_connection_context() as conn:
            try:
                with conn.cursor() as cursor:
                    for table_name in CATALOG_PRINT_TABLES:
                        cursor.execute(
                            f"SELECT id FROM {table_name} WHERE cardtrader_id = %s LIMIT 1",
                            (blueprint_id,),
                        )
                        result = cursor.fetchone()
                        if result:
                            return result["id"], table_name

                    return None
            except Exception as exc:
                logger.error(
                    "MySQL blueprint lookup failed for id=%s (%s)",
                    blueprint_id,
                    type(exc).__name__,
                )
                return None

    def map_blueprint_id(self, blueprint_id: int) -> Optional[Tuple[int, str]]:
        """
        Map CardTrader blueprint_id to Ebartex print_id and table name.
        
        Args:
            blueprint_id: CardTrader blueprint ID
            
        Returns:
            (print_id, table_name) tuple or None if not found
        """
        # Try cache first
        cached = self._get_from_cache(blueprint_id)
        if cached:
            return cached
        
        # Query MySQL
        result = self._query_mysql(blueprint_id)
        
        if result:
            print_id, table_name = result
            # Cache the result
            self._set_cache(blueprint_id, print_id, table_name)
            return result
        
        # Not found in database
        logger.warning(f"Blueprint {blueprint_id} not found in MySQL database")
        return None

    def batch_map_blueprint_ids(
        self, blueprint_ids: list[int]
    ) -> Dict[int, Optional[Tuple[int, str]]]:
        """
        Batch map multiple blueprint_ids.
        
        Args:
            blueprint_ids: List of CardTrader blueprint IDs
            
        Returns:
            Dictionary mapping blueprint_id -> (print_id, table_name) or None
        """
        results = {}
        uncached_ids = []
        
        # Check cache for all IDs
        for blueprint_id in blueprint_ids:
            cached = self._get_from_cache(blueprint_id)
            if cached:
                results[blueprint_id] = cached
            else:
                uncached_ids.append(blueprint_id)
        
        # Query MySQL for uncached IDs
        if uncached_ids:
            from app.core.database import get_mysql_connection_context
            
            with get_mysql_connection_context() as conn:
                try:
                    with conn.cursor() as cursor:
                        # Build UNION query for all catalog tables
                        placeholders = ",".join(["%s"] * len(uncached_ids))

                        query = " UNION ".join(
                            f"SELECT id, '{table_name}' as table_name, cardtrader_id "
                            f"FROM {table_name} WHERE cardtrader_id IN ({placeholders})"
                            for table_name in CATALOG_PRINT_TABLES
                        )

                        # Execute with all IDs repeated for each UNION branch
                        params = uncached_ids * len(CATALOG_PRINT_TABLES)
                        cursor.execute(query, params)
                        
                        for row in cursor.fetchall():
                            blueprint_id = row["cardtrader_id"]
                            print_id = row["id"]
                            table_name = row["table_name"]
                            results[blueprint_id] = (print_id, table_name)
                            # Cache the result
                            self._set_cache(blueprint_id, print_id, table_name)
                        
                        # Mark missing IDs as None
                        for blueprint_id in uncached_ids:
                            if blueprint_id not in results:
                                results[blueprint_id] = None
                except Exception as exc:
                    logger.error("MySQL batch blueprint lookup failed (%s)", type(exc).__name__)
                    # Fallback to individual queries
                    for blueprint_id in uncached_ids:
                        if blueprint_id not in results:
                            results[blueprint_id] = self.map_blueprint_id(blueprint_id)
        
        return results


# Global instance
_blueprint_mapper: Optional[BlueprintMapper] = None


def get_blueprint_mapper() -> BlueprintMapper:
    """Get or create global blueprint mapper instance."""
    global _blueprint_mapper
    if _blueprint_mapper is None:
        _blueprint_mapper = BlueprintMapper()
    return _blueprint_mapper
