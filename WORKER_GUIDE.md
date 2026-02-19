# 🔄 Guida Riavvio Worker Celery

## 📋 Indice
1. [Riavvio Base](#riavvio-base)
2. [Riavvio con Logs](#riavvio-con-logs)
3. [Riavvio con Monitoraggio](#riavvio-con-monitoraggio)
4. [Riavvio Multi-Worker](#riavvio-multi-worker)
5. [Troubleshooting](#troubleshooting)

---

## 🚀 Riavvio Base

### Metodo 1: Riavvio Manuale

```bash
# 1. Ferma il worker corrente (Ctrl+C nel terminale dove è in esecuzione)

# 2. Attiva il virtual environment
cd /Users/julianrovera/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync
source venv/bin/activate

# 3. Riavvia il worker
celery -A app.tasks.celery_app worker --loglevel=info -Q bulk-sync,high-priority,default
```

### Metodo 2: Kill Process e Riavvio

```bash
# 1. Trova il processo Celery
ps aux | grep celery

# 2. Kill il processo (sostituisci PID con il numero del processo)
kill -9 <PID>

# 3. Riavvia come sopra
cd /Users/julianrovera/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync
source venv/bin/activate
celery -A app.tasks.celery_app worker --loglevel=info -Q bulk-sync,high-priority,default
```

---

## 📊 Riavvio con Logs

Per vedere i log in tempo reale e salvarli in un file:

```bash
cd /Users/julianrovera/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync
source venv/bin/activate

# Avvia worker con log in file
celery -A app.tasks.celery_app worker \
    --loglevel=info \
    -Q bulk-sync,high-priority,default \
    --logfile=logs/celery_worker.log \
    --pidfile=logs/celery_worker.pid
```

**Nota**: Assicurati che la cartella `logs/` esista:
```bash
mkdir -p logs
```

---

## 🔍 Riavvio con Monitoraggio

Per monitorare lo stato dei task in tempo reale:

### Terminale 1: Worker
```bash
cd /Users/julianrovera/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync
source venv/bin/activate
celery -A app.tasks.celery_app worker --loglevel=info -Q bulk-sync,high-priority,default
```

### Terminale 2: Flower (Monitor Web UI)
```bash
cd /Users/julianrovera/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync
source venv/bin/activate

# Installa Flower se non presente
pip install flower

# Avvia Flower
celery -A app.tasks.celery_app flower --port=5555
```

Poi apri: http://localhost:5555

---

## ⚡ Riavvio Multi-Worker

Per processare più task in parallelo:

```bash
cd /Users/julianrovera/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync
source venv/bin/activate

# Worker 1: Solo bulk-sync (lavori pesanti)
celery -A app.tasks.celery_app worker \
    --loglevel=info \
    -Q bulk-sync \
    --concurrency=2 \
    --hostname=worker1@%h

# Worker 2: High priority e default (lavori veloci)
celery -A app.tasks.celery_app worker \
    --loglevel=info \
    -Q high-priority,default \
    --concurrency=4 \
    --hostname=worker2@%h
```

**Nota**: Apri due terminali separati per ogni worker.

---

## 🛠️ Troubleshooting

### Worker non si avvia

**Errore**: `ModuleNotFoundError: No module named 'app'`

**Soluzione**:
```bash
# Assicurati di essere nella directory corretta
cd /Users/julianrovera/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync

# Verifica che il virtual environment sia attivo
which python  # Dovrebbe mostrare il path a venv/bin/python

# Reinstalla le dipendenze
source venv/bin/activate
pip install -r requirements.txt
```

---

### Worker si blocca o non processa task

**Diagnosi**:
```bash
# Verifica che Redis sia attivo
redis-cli ping  # Dovrebbe rispondere "PONG"

# Verifica che il worker sia connesso a Redis
redis-cli
> KEYS celery*
> EXIT
```

**Soluzione**:
```bash
# Riavvia Redis (se locale)
brew services restart redis

# Oppure riavvia il worker
# (Ctrl+C e poi riavvia come sopra)
```

---

### Task rimangono in PENDING

**Possibili cause**:
1. Worker non in esecuzione
2. Redis non raggiungibile
3. Queue name non corrispondente

**Verifica**:
```bash
# Controlla che il worker stia ascoltando le queue corrette
celery -A app.tasks.celery_app inspect active_queues
```

**Soluzione**: Riavvia il worker con le queue corrette:
```bash
celery -A app.tasks.celery_app worker --loglevel=info -Q bulk-sync,high-priority,default
```

---

### Worker consuma troppa memoria

**Soluzione**: Limita la concorrenza:
```bash
celery -A app.tasks.celery_app worker \
    --loglevel=info \
    -Q bulk-sync,high-priority,default \
    --concurrency=2  # Riduce il numero di worker process
```

---

## 📝 Script di Riavvio Rapido

Crea uno script `restart_worker.sh`:

```bash
#!/bin/bash

cd /Users/julianrovera/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync

# Kill worker esistenti
pkill -f "celery.*celery_app.*worker"

# Attendi 2 secondi
sleep 2

# Attiva venv e riavvia
source venv/bin/activate
celery -A app.tasks.celery_app worker --loglevel=info -Q bulk-sync,high-priority,default
```

Rendi eseguibile:
```bash
chmod +x restart_worker.sh
```

Esegui:
```bash
./restart_worker.sh
```

---

## ✅ Verifica Worker Attivo

Dopo il riavvio, verifica che il worker sia attivo:

```bash
# Metodo 1: Controlla i processi
ps aux | grep celery

# Metodo 2: Usa Celery inspect
celery -A app.tasks.celery_app inspect active

# Metodo 3: Controlla le statistiche
celery -A app.tasks.celery_app inspect stats
```

---

## 🔗 Link Utili

- **Frontend Test**: http://localhost:8000/static/index.html
- **API Docs**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health
- **Flower (se installato)**: http://localhost:5555

---

## 📌 Note Importanti

1. **Sempre attiva il virtual environment** prima di avviare il worker
2. **Verifica che Redis sia attivo** prima di riavviare
3. **Usa Ctrl+C** per fermare il worker (non kill -9 se possibile)
4. **Controlla i log** se qualcosa non funziona
5. **Un solo worker per queue** è sufficiente per test locali
