# Logs Directory

Questo directory contiene i log del microservizio BRX Sync.

## File di Log

- `brx_sync.log`: Log dettagliati di tutte le operazioni, inclusi:
  - Creazione e chiusura di event loops
  - Esecuzione di task Celery
  - Chiamate API a CardTrader
  - Errori e warning
  - Stato delle sincronizzazioni

## Formato dei Log

I log sono in formato JSON, una riga per entry:

```json
{
  "timestamp": "2026-02-19T18:50:24.123456",
  "message": "Celery task sync_update_product_to_cardtrader started",
  "data": {
    "user_id": "db24fb13-ec73-49b8-932c-f0043dd47e86",
    "item_id": 37,
    "task_id": "e4396bc3-c466-4228-8590-f1a68d073b4c"
  }
}
```

## Consultazione Log

Per visualizzare i log in tempo reale:

```bash
tail -f logs/brx_sync.log
```

Per filtrare per tipo di messaggio:

```bash
grep "sync_update" logs/brx_sync.log
```

Per convertire in formato leggibile:

```bash
cat logs/brx_sync.log | jq '.'
```

## Rotazione Log

I log non vengono ruotati automaticamente. Per evitare che il file diventi troppo grande, puoi:

1. Spostare il file corrente:
   ```bash
   mv logs/brx_sync.log logs/brx_sync.log.$(date +%Y%m%d)
   ```

2. Il sistema creerà automaticamente un nuovo file al prossimo log.

## Note

- I log vengono scritti in modo asincrono e sicuro
- Se la scrittura fallisce, il sistema continua a funzionare (non blocca le operazioni)
- I log contengono informazioni sensibili (token, ID utenti) - non condividerli pubblicamente
