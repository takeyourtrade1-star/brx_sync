"""
Blueprint ID mapper: maps CardTrader blueprint_id to Ebartex print_id (MySQL).
Uses Redis cache for performance.
"""
import logging
from typing import Optional, Tuple, Dict

from app.core.database import get_mysql_connection
from app.core.redis_client import get_redis_sync

logger = logging.getLogger(__name__)


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
                if len(parts) == 2:
                    return int(parts[0]), parts[1]
            except (ValueError, IndexError):
                logger.warning(f"Invalid cache format for blueprint {blueprint_id}: {cached}")
        
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
                    # Query all print tables for the blueprint_id
                    # Priority: cards_prints, op_prints, pk_prints, sealed_products
                    
                    # Try cards_prints (MTG)
                    cursor.execute(
                        "SELECT id FROM cards_prints WHERE cardtrader_id = %s LIMIT 1",
                        (blueprint_id,)
                    )
                    result = cursor.fetchone()
                    if result:
                        return result["id"], "cards_prints"
                    
                    # Try op_prints (One Piece)
                    cursor.execute(
                        "SELECT id FROM op_prints WHERE cardtrader_id = %s LIMIT 1",
                        (blueprint_id,)
                    )
                    result = cursor.fetchone()
                    if result:
                        return result["id"], "op_prints"
                    
                    # Try pk_prints (Pokemon)
                    cursor.execute(
                        "SELECT id FROM pk_prints WHERE cardtrader_id = %s LIMIT 1",
                        (blueprint_id,)
                    )
                    result = cursor.fetchone()
                    if result:
                        return result["id"], "pk_prints"
                    
                    # Try sealed_products
                    cursor.execute(
                        "SELECT id FROM sealed_products WHERE cardtrader_id = %s LIMIT 1",
                        (blueprint_id,)
                    )
                    result = cursor.fetchone()
                    if result:
                        return result["id"], "sealed_products"
                    
                    return None
            except Exception as e:
                logger.error(f"Error querying MySQL for blueprint {blueprint_id}: {e}")
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
                        # Build UNION query for all tables
                        placeholders = ",".join(["%s"] * len(uncached_ids))
                        
                        query = f"""
                        SELECT id, 'cards_prints' as table_name, cardtrader_id
                        FROM cards_prints
                        WHERE cardtrader_id IN ({placeholders})
                        UNION
                        SELECT id, 'op_prints' as table_name, cardtrader_id
                        FROM op_prints
                        WHERE cardtrader_id IN ({placeholders})
                        UNION
                        SELECT id, 'pk_prints' as table_name, cardtrader_id
                        FROM pk_prints
                        WHERE cardtrader_id IN ({placeholders})
                        UNION
                        SELECT id, 'sealed_products' as table_name, cardtrader_id
                        FROM sealed_products
                        WHERE cardtrader_id IN ({placeholders})
                        """
                        
                        # Execute with all IDs repeated for each UNION
                        params = uncached_ids * 4
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
                except Exception as e:
                    logger.error(f"Error batch querying MySQL: {e}")
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
