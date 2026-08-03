# 🚀 PASSO 5 - Setup Python + MySQL Hostinger

## 📋 PASSO 5A: Configura MySQL Hostinger

Apri il Terminale e vai nella directory:

```bash
cd ~/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync
```

Crea il file `.env`:

```bash
nano .env
```

Oppure con TextEdit:

```bash
open -a TextEdit .env
```

**Copia e incolla questo contenuto, poi modifica i valori MySQL con i tuoi dati Hostinger:**

```env
# Database PostgreSQL (locale)
DATABASE_URL=postgresql+asyncpg://$(whoami)@localhost:5432/brx_sync_db

# MySQL Hostinger (per blueprint mapping)
MYSQL_HOST=srv1502.hstgr.io
MYSQL_PORT=3306
MYSQL_USER=u792485705_final
MYSQL_PASSWORD=7rWolwcD|
MYSQL_DATABASE=u792485705_mtgfinal

# Redis (locale)
REDIS_URL=redis://localhost:6379/0

# Fernet Key (genereremo dopo)
FERNET_KEY=

# Application
DEBUG=true
ENVIRONMENT=development
PROJECT_NAME=BRX Sync
APP_NAME=brx-sync
APP_VERSION=1.0.0

# CardTrader API
CARDTRADER_API_BASE_URL=https://api.cardtrader.com/api/v2

# Rate Limiting
RATE_LIMIT_REQUESTS=200
RATE_LIMIT_WINDOW_SECONDS=10
```

**⚠️ IMPORTANTE:** 
- Sostituisci `$(whoami)` in DATABASE_URL con il tuo username Mac (es: `julianrovera`)
- Oppure esegui: `echo $(whoami)` per vedere il tuo username

**💾 Salva il file:**
- In nano: `Ctrl+X`, poi `Y`, poi `Enter`
- In TextEdit: `Cmd+S`

---

## 📋 PASSO 5B: Setup Python Environment

Torna nel Terminale e esegui:

```bash
# Assicurati di essere nella directory corretta
cd ~/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync

# Crea virtual environment
python3 -m venv venv

# Attiva virtual environment
source venv/bin/activate
```

**✅ Vedrai `(venv)` prima del prompt!**

---

## 📋 PASSO 5C: Installa Dipendenze

Con il virtual environment attivo:

```bash
# Usa la stessa versione verificata dalla build/CI
python -m pip install --upgrade pip==26.2

# Installa tutte le dipendenze
python -m pip install --requirement requirements.txt
```

**⏳ Questo richiederà qualche minuto...**

---

## 📋 PASSO 6: Genera Chiave Fernet

Con il virtual environment ancora attivo:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**📝 COPIA questa chiave!** (sarà una stringa lunga tipo: `gAAAAABl...`)

---

## 📋 PASSO 7: Aggiungi Fernet Key al .env

Apri di nuovo il file `.env`:

```bash
nano .env
```

Trova la riga:
```
FERNET_KEY=
```

E sostituiscila con:
```
FERNET_KEY=LA_TUA_CHIAVE_GENERATA
```

**💾 Salva il file**

---

## 📋 PASSO 8: Crea Schema Database PostgreSQL

Con il virtual environment attivo:

```bash
# Crea il database (se non esiste già)
createdb brx_sync_db

# Esegui lo schema SQL
psql -d brx_sync_db -f schema.sql
```

**✅ Se non vedi errori, lo schema è creato!**

Verifica:

```bash
psql -d brx_sync_db -c "\dt"
```

Dovresti vedere 3 tabelle:
- `user_sync_settings`
- `user_inventory_items`  
- `sync_operations`

---

## 📋 PASSO 9: Test Connessioni

Con il virtual environment attivo:

```bash
python test_local.py
```

**✅ Dovresti vedere:**
- ✓ PostgreSQL: Connesso
- ✓ MySQL: Connesso
- ✓ Redis: Connesso
- ✓ Encryption: Funziona correttamente

**Se vedi errori, dimmi quali!**

---

## 📋 PASSO 10: Avvia il Server

Con il virtual environment attivo:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**✅ Dovresti vedere:**
```
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     Application startup complete.
```

**🎉 Il server è avviato!**

---

## 📋 PASSO 11: Apri il Frontend

Nel browser, apri:

```
http://localhost:8000/static/index.html
```

**🎉 Se vedi l'interfaccia, tutto funziona!**

---

## 🐛 Se hai errori

### Errore: "psql: command not found"
```bash
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
```

### Errore: "database does not exist"
```bash
createdb brx_sync_db
```

### Errore MySQL: "connection refused" o "access denied"
- Verifica che MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD siano corretti
- Verifica che Hostinger permetta connessioni remote dal tuo IP

### Errore: "FERNET_KEY not configured"
- Assicurati di aver aggiunto la chiave generata al .env

---

## ✅ Checklist

- [ ] File `.env` creato con MySQL Hostinger
- [ ] Virtual environment creato e attivato
- [ ] Dipendenze installate
- [ ] Chiave Fernet generata e aggiunta a .env
- [ ] Database PostgreSQL creato
- [ ] Schema SQL eseguito
- [ ] Test connessioni passati
- [ ] Server avviato
- [ ] Frontend accessibile

**Dimmi quando sei arrivato al PASSO 9 (test connessioni) e ti aiuto se ci sono problemi!** 🚀
