# Fase 0 — fondazioni inventario scambi

## Verifiche lampo (2026-07-14)

- Connessione del container production `auction-api`: database attivo
  `ebartex_auth_db`; nello stesso schema sono presenti sia le tabelle auction
  (`auctions`, `bids`, `orders`, `order_status_history`, `notifications`) sia
  `user_inventory_items`.
- Vincolo reale di `user_inventory_items.user_id`:
  `FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE`.
  Non punta a `user_sync_settings`: `/internal/credit` può quindi accreditare
  un utente registrato anche se non ha mai collegato CardTrader.

Le verifiche sono state eseguite in sola lettura dal container auction. Nessuna
migrazione o modifica è stata applicata in produzione.

## Migrazione

Applicare `migrations/20260714_trade_inventory_foundations.sql` prima di avviare
questa versione dell'API. La migrazione:

1. aggiunge e backfilla `user_inventory_items.source`;
2. aggiunge il registro idempotente `inventory_ops`;
3. aggiunge l'indice per il filtro dell'inventario scambi.

Backfill: righe con `external_stock_id` diventano `cardtrader`; le righe NULL già
esistenti diventano `internal_test`. Solo `/internal/credit` crea righe `trade`.

## API privata

Gli endpoint `POST /internal/reservations`,
`POST /internal/reservations/release` e `POST /internal/credit` richiedono
`X-Internal-Token`. Il servizio fallisce chiuso se `INTERNAL_API_TOKEN` manca.

## Verifica locale

- Migrazione applicata due volte su PostgreSQL 16 usa-e-getta: backfill e DDL
  idempotenti.
- Suite completa del repo: 11 test verdi, inclusi concorrenza, compensazione
  CardTrader a metà batch, replay, fallback CT cancellato e reconciler.
- Ruff e mypy verdi sui file della Fase 0; immagine Docker locale costruita.
- Smoke HTTP reale sui tre endpoint: auth senza token `401`, flusso
  credit → reserve → release riuscito e replay idempotente per tutte le POST.
