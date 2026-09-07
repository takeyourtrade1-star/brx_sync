# Recupero automatico del catalogo CardTrader

L'inventario conserva ogni prodotto Magic valido dell'export CardTrader, anche
quando il blueprint non esiste ancora nel catalogo MySQL. Le quantità restano
visibili nell'inventario personale; gli oggetti senza mapping verificato non
possono essere riservati, promossi nel marketplace o scambiati.

Nella stessa transazione dell'osservazione dello stock si registra una richiesta
di recupero. Il job è unico per provider, gioco e blueprint; le richieste dei
diversi utenti condividono il lavoro. Il worker legge il blueprint dall'espansione
esatta CardTrader e la carta dall'UUID Scryfall esatto, aggiorna `cards`, `sets` e
`cards_prints`, poi registra un outbox PostgreSQL. L'identificativo della ricerca è
`mtg_<cards_prints.id>`, distinto dal blueprint CardTrader.

Solo la conferma positiva del task Meilisearch permette di finalizzare il mapping.
La finalizzazione modifica i metadati dell'inventario e verifica il profilo e la
versione della modalità correnti; non riscrive quantità, riserve o prezzi. Le
riconciliazioni successive non aggirano un outbox ancora da confermare. Retry,
lease scaduti ed errori restano registrati nel database.

## Configurazione di produzione

Applicare `migrations/20260907_catalog_import_queue.sql` tramite il ruolo migration
prima di avviare API e worker aggiornati. Lo startup esegue questa sequenza.
Per attivare il processo impostare esplicitamente:

```text
CATALOG_IMPORT_ENABLED=true
CATALOG_MYSQL_WRITE_ENABLED=true
CATALOG_SEARCH_PUBLISH_ENABLED=true
CARDTRADER_WRITES_ENABLED=false
```

Il worker principale pianifica i job. Il servizio separato
`brx-sync-catalog-worker` consuma soltanto `catalog-import,catalog-index`, senza
un secondo scheduler. È l'unico container che riceve le nuove credenziali:

| Parametro SSM sotto `/prod/ebartex/` | Utilizzo |
| --- | --- |
| `catalog_mysql_writer_user` | Ruolo MySQL dedicato al catalogo |
| `catalog_mysql_writer_password` | Password SecureString |
| `catalog_meilisearch_url` | Endpoint della ricerca |
| `catalog_meilisearch_key` | Chiave SecureString limitata all'indice `cards` |

Il writer MySQL richiede TLS e solamente SELECT, INSERT, UPDATE su `cards`,
`sets`, `cards_prints`; nessun privilegio sullo stock degli utenti. La chiave
Meilisearch richiede `search`, `documents.get`, `documents.add`, `tasks.get`.
Il worker non cancella documenti: gli identificativi legacy possono essere ancora
usati da link salvati e preferiti. Il lookup frontend gestisce gli alias senza
perdere altri blueprint nel batch.

La chiave dedicata predisposta il 7 settembre 2026 scade il **6 dicembre 2026 alle
11:35:35 UTC**. Prima di quella data va sostituita con una chiave dello stesso
ambito, aggiornata in SSM e ricaricata nel worker. Non inserire master key o chiavi
con permessi di cancellazione nel runtime. Dopo la rotazione verificare una
pubblicazione e il suo ACK, senza stampare segreti nei log.

## Verifica e recupero operativo

Confrontare il prodotto CT per `external_stock_id` e la sua `quantity`; il numero
delle righe e la somma delle copie sono metriche differenti. Per una verifica
completa usare un nuovo export GET, rispettando la modalità corrente del profilo.
Gli oggetti storici assenti non si eliminano in blocco: il reconciler applica la
conferma di assenza e le protezioni su versioni, riserve e webhook.

Le query seguenti non contengono credenziali e sono di sola lettura:

```sql
SELECT status, count(*) FROM catalog_import_jobs GROUP BY status;
SELECT status, count(*) FROM catalog_index_outbox GROUP BY status;
SELECT id, blueprint_id, attempts, last_error_code, last_error
FROM catalog_import_jobs
WHERE status = 'needs_review'
ORDER BY updated_at;
```

Un errore di identità richiede verifica dei provider ID; non correggerlo cercando
solo per nome. Un errore temporaneo mantiene il job o l'outbox recuperabile. Se
MySQL ha già confermato l'inserimento, un retry riutilizza lo stesso print e riprende
la pubblicazione senza raddoppiare le quantità. Dopo un intervento manuale su una
causa definitiva, rivalutare il singolo job prima di rimetterlo in coda.

Il comando frontend storico `catalog-sync/backfill --apply` è disabilitato perché
scriveva soltanto nella ricerca, lasciando assente il catalogo MySQL. I comandi di
analisi verificano adesso gli ID dei blueprint, includendo stampe con lo stesso nome.
