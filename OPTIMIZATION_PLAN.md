# Piano Ottimizzazione Sistema Sincronizzazione

## Obiettivo
Ridurre il tempo di sincronizzazione da **5-10 minuti** a **1-2 minuti** per 250.000 carte, rendendo il sistema robusto e scalabile.

## Analisi Performance Attuale

### Bottleneck Identificati

1. **Processing Sequenziale Chunk**: I 50 chunk vengono processati uno alla volta
2. **SELECT Individuali**: Per ogni prodotto (250k) viene fatta una SELECT per verificare esistenza
3. **INSERT/UPDATE Individuali**: Ogni prodotto viene inserito/aggiornato singolarmente
4. **Commit per Chunk**: 50 commit separati invece di batch più grandi
5. **Log Debug Eccessivi**: Scritture su file per ogni prodotto rallentano l'esecuzione

## Ottimizzazioni da Implementare

### 1. Batch SELECT per Verifica Esistenza
**Problema**: 250.000 SELECT individuali
**Soluzione**: Una singola query con `WHERE (user_id, blueprint_id, external_stock_id) IN (...)`
**Guadagno**: ~100x più veloce

### 2. Batch INSERT/UPDATE con SQLAlchemy Bulk Operations
**Problema**: 250.000 INSERT/UPDATE individuali
**Soluzione**: Usare `bulk_insert_mappings()` e `bulk_update_mappings()`
**Guadagno**: ~50x più veloce

### 3. Indice Composito Ottimizzato
**Problema**: Query di lookup usa 3 colonne separate
**Soluzione**: Indice composito `(user_id, blueprint_id, external_stock_id)`
**Guadagno**: Query 10x più veloci

### 4. Parallelizzazione Chunk (Opzionale)
**Problema**: Chunk processati sequenzialmente
**Soluzione**: Processare 3-5 chunk in parallelo con `asyncio.gather()`
**Guadagno**: 3-5x più veloce (con limiti per non sovraccaricare DB)

### 5. Commit Ottimizzato
**Problema**: 50 commit separati
**Soluzione**: Commit ogni 2-3 chunk o alla fine
**Guadagno**: Riduce overhead transazionale

### 6. Rimozione Log Debug
**Problema**: Scritture su file per ogni prodotto
**Soluzione**: Log solo a livello chunk o rimozione completa
**Guadagno**: Elimina I/O overhead

### 7. Progress Tracking
**Aggiunta**: Endpoint per monitorare progresso sincronizzazione
**Beneficio**: UX migliore per l'utente

## Stima Performance Dopo Ottimizzazioni

- **Export CardTrader**: 2-3 minuti (invariato, dipende da CardTrader)
- **Processing Ottimizzato**: 
  - Batch SELECT: ~5-10 secondi (invece di 50+ minuti)
  - Batch INSERT/UPDATE: ~10-20 secondi (invece di 20+ minuti)
  - Totale processing: **15-30 secondi** (invece di 70+ minuti)
- **Totale**: **2.5-3.5 minuti** per 250.000 carte

## Implementazione

### Fase 1: Ottimizzazioni Database
1. Aggiungere indice composito
2. Verificare indici esistenti

### Fase 2: Batch Operations
1. Refactoring `_process_products_chunk` per batch SELECT
2. Implementare batch INSERT/UPDATE
3. Ottimizzare commit strategy

### Fase 3: Rimozione Overhead
1. Rimuovere log debug eccessivi
2. Ottimizzare blueprint mapper batch queries

### Fase 4: Progress Tracking
1. Aggiungere progress updates nel sync operation
2. Endpoint per query progress

### Fase 5: Testing e Monitoring
1. Test con dataset reale
2. Monitoring performance metrics
