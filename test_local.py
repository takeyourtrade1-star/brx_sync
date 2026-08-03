"""
Script di test locale per verificare connessioni e funzionalità base.
"""
import asyncio
import sys
from uuid import uuid4

from app.core.config import get_settings
from app.core.crypto import get_encryption_manager
from app.core.database import get_db_session_context, get_mysql_connection
from app.core.redis_client import get_redis
from app.services.cardtrader_client import CardTraderClient


async def test_connections():
    """Test connessioni a database e Redis."""
    print("=" * 60)
    print("TEST CONNESSIONI")
    print("=" * 60)
    
    settings = get_settings()
    print(f"\n✓ Settings caricati: {settings.APP_NAME} v{settings.APP_VERSION}")
    
    # Test PostgreSQL
    try:
        async with get_db_session_context() as session:
            from sqlalchemy import text
            result = await session.execute(text("SELECT 1 as test"))
            row = result.scalar()
            print(f"✓ PostgreSQL: Connesso (test query: {row})")
    except Exception as e:
        print(f"✗ PostgreSQL: ERRORE - {e}")
        return False
    
    # Test MySQL
    try:
        conn = get_mysql_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 as test")
            result = cursor.fetchone()
            print(f"✓ MySQL: Connesso (test query: {result['test']})")
    except Exception as e:
        print(f"✗ MySQL: ERRORE - {e}")
        return False
    
    # Test Redis
    try:
        redis = await get_redis()
        if redis:
            await redis.ping()
            print("✓ Redis: Connesso")
        else:
            print("✗ Redis: Non disponibile")
            return False
    except Exception as e:
        print(f"✗ Redis: ERRORE - {e}")
        return False
    
    # Test Encryption
    try:
        enc_manager = get_encryption_manager()
        test_text = "test_secret"
        encrypted = enc_manager.encrypt(test_text)
        decrypted = enc_manager.decrypt(encrypted)
        if decrypted == test_text:
            print("✓ Encryption: Funziona correttamente")
        else:
            print("✗ Encryption: Decrypt fallito")
            return False
    except Exception as e:
        print(f"✗ Encryption: ERRORE - {e}")
        return False
    
    print("\n✅ Tutte le connessioni funzionano!")
    return True


async def test_cardtrader_api(token: str):
    """Test connessione a CardTrader API."""
    print("\n" + "=" * 60)
    print("TEST CARDTRADER API")
    print("=" * 60)
    
    if not token:
        print("⚠ Token CardTrader non fornito. Salto test API.")
        return
    
    try:
        test_user_id = str(uuid4())
        async with CardTraderClient(token, test_user_id) as client:
            # Test /info endpoint
            print("\n📡 Testando endpoint /info...")
            info = await client.get_info()
            print(f"✓ App ID: {info.get('id')}")
            print(f"✓ App Name: {info.get('name')}")
            print("✓ Shared Secret: ricevuto (valore non stampato)")
            
            # Test /expansions/export
            print("\n📡 Testando endpoint /expansions/export...")
            expansions = await client.get_expansions_export()
            print(f"✓ Trovate {len(expansions)} expansions")
            if expansions:
                print(f"  Esempio: {expansions[0].get('name', 'N/A')}")
            
            # Test /products/export (solo count, non tutti i prodotti)
            print("\n📡 Testando endpoint /products/export (primi 10)...")
            products = await client.get_products_export()
            print(f"✓ Trovati {len(products)} prodotti totali")
            if products:
                sample = products[0]
                print(f"  Esempio prodotto:")
                print(f"    - ID: {sample.get('id')}")
                print(f"    - Blueprint ID: {sample.get('blueprint_id')}")
                print(f"    - Nome: {sample.get('name_en', 'N/A')}")
                print(f"    - Quantità: {sample.get('quantity', 0)}")
                print(f"    - Prezzo: {sample.get('price_cents', 0)} centesimi")
            
            print("\n✅ CardTrader API funziona correttamente!")
            
    except Exception as e:
        print(f"\n✗ ERRORE CardTrader API: {e}")
        import traceback
        traceback.print_exc()


async def test_blueprint_mapper():
    """Test mapping blueprint_id."""
    print("\n" + "=" * 60)
    print("TEST BLUEPRINT MAPPER")
    print("=" * 60)
    
    from app.services.blueprint_mapper import get_blueprint_mapper
    
    mapper = get_blueprint_mapper()
    
    # Test con alcuni blueprint_id comuni (esempio)
    test_blueprint_ids = [1, 2, 3, 100, 1000]
    
    print(f"\n🔍 Testando mapping per blueprint_ids: {test_blueprint_ids}")
    
    for blueprint_id in test_blueprint_ids:
        result = mapper.map_blueprint_id(blueprint_id)
        if result:
            print_id, table_name = result
            print(f"  ✓ Blueprint {blueprint_id} → Print ID {print_id} ({table_name})")
        else:
            print(f"  ⚠ Blueprint {blueprint_id} → Non trovato nel database")


async def main():
    """Main test function."""
    print("\n" + "🚀 " * 20)
    print("BRX SYNC - TEST LOCALE")
    print("🚀 " * 20 + "\n")
    
    # Test connessioni base
    if not await test_connections():
        print("\n❌ Test connessioni fallito. Verifica la configurazione.")
        sys.exit(1)
    
    # Test CardTrader API (opzionale, richiede token)
    import os
    cardtrader_token = os.getenv("CARDTRADER_TOKEN")
    if cardtrader_token:
        await test_cardtrader_api(cardtrader_token)
    else:
        print("\n⚠ CARDTRADER_TOKEN non impostato. Salto test API.")
        print("   Per testare l'API, esporta: export CARDTRADER_TOKEN='your_token'")
    
    # Test blueprint mapper
    await test_blueprint_mapper()
    
    print("\n" + "=" * 60)
    print("✅ TEST COMPLETATI")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
