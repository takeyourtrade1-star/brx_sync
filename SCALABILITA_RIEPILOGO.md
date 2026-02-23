# Scalabilità: migliaia di utenti e richieste

## Risposta breve

**Sì, il sistema è progettato per scalare** e può gestire migliaia di utenti senza crash, **a patto di**:

- usare **più worker Celery** in produzione,
- mantenere **Redis e PostgreSQL** con risorse adeguate,
- (opzionale) disabilitare il log su file in produzione per evitare contesa tra worker.

Senza questi accorgimenti, sotto carico molto alto si rischiano code lunghe e timeout, non necessariamente crash.

---

## Cosa è già a posto

| Componente | Comportamento |
|------------|----------------|
| **Rate limit** | Per **utente** (Redis `rate_limit:{user_id}`). Migliaia di utenti = migliaia di bucket indipendenti. CardTrader non viene saturato da un singolo utente. |
| **Redis** | Connection **pool** sync (max 50 connessioni per processo). I worker Celery riusano le connessioni. |
| **PostgreSQL** | Pool 50 + overflow 100 (configurabile). Adatto a molti task concorrenti. |
| **MySQL** (blueprint) | Pool 20 + overflow 20. Usato in lettura per il mapping. |
| **Celery** | Code separate (bulk-sync vs high-priority), `worker_prefetch_multiplier=1`, retry con backoff. I task non si “rubano” la coda. |
| **CardTrader** | Limite rispettato **per utente**; adaptive rate limiter riduce i 429. |

Quindi l’architettura è **scalabile**: più utenti e più richieste si gestiscono con **più worker** e **più risorse** su Redis/DB, senza dover cambiare la logica.

---

## Cosa fare in produzione (per migliaia di utenti)

1. **Celery worker**
   - Avviare **più processi worker** (es. 4–8 o più su una macchina, o più macchine).
   - Esempio: `celery -A app.tasks.celery_app worker -c 4 -Q high-priority,bulk-sync,default -l info`

2. **Redis**
   - Redis regge bene molte connessioni; verificare `max_connections` e risorse (RAM/CPU) in base al numero di worker e all’uso della cache/rate limit.

3. **PostgreSQL**
   - Pool già 50+100. Se hai molti worker (es. 10+), il totale connessioni può crescere; adeguare `max_connections` del server Postgres e, se serve, `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` (vedi `SCALABILITY_ANALYSIS.md`).

4. **Log su file (opzionale ma consigliato sotto carico)**
   - Con molti worker, scrivere tutti sullo stesso file `logs/brx_sync.log` può creare contesa e I/O non necessario.
   - In produzione puoi disabilitare il log su file e usare solo i log standard (stdout/logger):
   - **`.env`**: `SYNC_LOG_TO_FILE=false`
   - I task continuano a usare `logger.info`/`logger.warning`; solo `_log_to_file()` viene saltata.

5. **Monitoraggio**
   - Monitorare: lunghezza code Celery, utilizzo pool DB, errori 429 CardTrader, latenza Redis. Così vedi subito se serve scalare ancora (più worker o più risorse).

---

## Rischio crash?

- **Crash improvvisi** sono poco probabili se Redis e Postgres sono su e configurati correttamente.
- I rischi reali sotto carico sono:
  - **Code Celery che crescono** (troppe richieste, pochi worker) → risposta lenta, non crash.
  - **Pool DB esauriti** → timeout/errori sulle richieste, non necessariamente kill del processo.
  - **Rate limit CardTrader** → gestito con retry e adaptive limiter; in casi estremi i task vanno in retry, non fanno cadere il servizio.

Quindi: **sì, è scalabile anche con migliaia di utenti e richieste**, con i giusti parametri e monitoraggio; il rischio principale è **latenza e code**, non crash. Per i dettagli tecnici (pool, Redis, MySQL, isolated engine) vedi `SCALABILITY_ANALYSIS.md`.
