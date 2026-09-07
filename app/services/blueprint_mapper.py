"""
Blueprint ID mapper: maps CardTrader blueprint_id to Ebartex print_id (MySQL).
Uses Redis cache for performance.
"""
import logging
from typing import Optional, Tuple, Dict

from app.core.redis_client import get_redis_sync

logger = logging.getLogger(__name__)


class BlueprintMapper:
    """Maps CardTrader blueprint_id to Ebartex print_id and table name."""

    CACHE_TTL = 86400  # 24 hours
    CACHE_PREFIX = "blueprint_mapping:v2:"

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
                if isinstance(cached, bytes):
                    cached = cached.decode("ascii")
                parts = cached.split(":", 1)
                if len(parts) == 2 and parts[1] in {
                    "cards_prints", "op_prints", "pk_prints", "sealed_products"
                } and int(parts[0]) > 0:
                    return int(parts[0]), parts[1]
            except (ValueError, IndexError, UnicodeDecodeError):
                logger.warning("Invalid blueprint cache record; ignoring it")
        
        return None

    def _set_cache(self, blueprint_id: int, print_id: int, table_name: str) -> None:
        """Store mapping in Redis cache."""
        key = self._get_cache_key(blueprint_id)
        value = f"{print_id}:{table_name}"
        self.redis.setex(key, self.CACHE_TTL, value)

    def _query_many(self, blueprint_ids: list[int]) -> Dict[int, Optional[Tuple[int, str]]]:
        """Read exact identities; conflicting tables are not a valid mapping."""
        from app.core.database import get_mysql_connection_context

        ids = list(dict.fromkeys(blueprint_ids))
        results = {blueprint_id: None for blueprint_id in ids}
        if not ids:
            return results
        placeholders = ",".join(["%s"] * len(ids))
        tables = ("cards_prints", "op_prints", "pk_prints", "sealed_products")
        query = " UNION ALL ".join(
            f"SELECT id, '{table}' AS table_name, cardtrader_id FROM {table} "
            f"WHERE cardtrader_id IN ({placeholders})"
            for table in tables
        )
        try:
            with get_mysql_connection_context() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, ids * len(tables))
                    rows = cursor.fetchall()
            candidates: dict[int, set[Tuple[int, str]]] = {}
            for row in rows:
                candidates.setdefault(int(row["cardtrader_id"]), set()).add(
                    (int(row["id"]), row["table_name"])
                )
            for blueprint_id, matches in candidates.items():
                if len(matches) == 1:
                    results[blueprint_id] = next(iter(matches))
                else:
                    logger.error("Ambiguous catalog identity for blueprint id=%s", blueprint_id)
        except Exception as exc:
            logger.error("MySQL blueprint lookup failed (%s)", type(exc).__name__)
        return results

    def _query_mysql(self, blueprint_id: int) -> Optional[Tuple[int, str]]:
        return self._query_many([blueprint_id]).get(blueprint_id)

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
        
        # Single and batch reads share the same ambiguity policy.
        if uncached_ids:
            for blueprint_id, mapping in self._query_many(uncached_ids).items():
                results[blueprint_id] = mapping
                if mapping is not None:
                    self._set_cache(blueprint_id, *mapping)

        return results


# Global instance
_blueprint_mapper: Optional[BlueprintMapper] = None


def get_blueprint_mapper() -> BlueprintMapper:
    """Get or create global blueprint mapper instance."""
    global _blueprint_mapper
    if _blueprint_mapper is None:
        _blueprint_mapper = BlueprintMapper()
    return _blueprint_mapper
