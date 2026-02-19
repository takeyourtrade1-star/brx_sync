# 🍎 Setup BRX Sync su macOS - Guida Passo-Passo

## 📋 PASSO 1: Installa Homebrew (se non ce l'hai)

Apri il Terminale e esegui:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Verifica installazione:
```bash
brew --version
```

---

## 📋 PASSO 2: Installa PostgreSQL

```bash
brew install postgresql@16
```

Avvia PostgreSQL:
```bash
brew services start postgresql@16
```

Crea il database:
```bash
createdb brx_sync_db
```

Verifica:
```bash
psql -d brx_sync_db -c "SELECT version();"
```

**✅ Se vedi la versione di PostgreSQL, è tutto OK!**

---

## 📋 PASSO 3: Installa Redis

```bash
brew install redis
```

Avvia Redis:
```bash
brew services start redis
```

Verifica:
```bash
redis-cli ping
```

**✅ Se risponde "PONG", è tutto OK!**

---

## 📋 PASSO 4: MySQL (Opzionale per ora)

Se hai già MySQL installato e accessibile, salta questo passo.

Altrimenti, per test locale puoi usare Docker:

```bash
docker run -d --name mysql-test -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=test_db -p 3306:3306 mysql:8.0
```

**⚠️ Nota:** Per ora possiamo testare senza MySQL, il blueprint mapping lo aggiungiamo dopo.

---

## 📋 PASSO 5: Setup Python Environment

Vai nella directory del progetto:

```bash
cd ~/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync
```

Crea virtual environment:

```bash
python3 -m venv venv
```

Attiva virtual environment:

```bash
source venv/bin/activate
```

**✅ Vedrai `(venv)` prima del prompt!**

Installa dipendenze:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 📋 PASSO 6: Genera Chiave Fernet

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**📝 COPIA questa chiave! Ti servirà per il .env**

---

## 📋 PASSO 7: Configura .env

Crea il file `.env`:

```bash
cp .env.example .env
```

Apri `.env` con un editor:

```bash
nano .env
# oppure
open -a TextEdit .env
```

Modifica queste righe:

```env
# Database PostgreSQL (usa i valori di default se hai installato con Homebrew)
DATABASE_URL=postgresql+asyncpg://$(whoami)@localhost:5432/brx_sync_db

# MySQL (per ora usa valori di test, lo configuriamo dopo)
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=root
MYSQL_DATABASE=test_db

# Redis (default va bene)
REDIS_URL=redis://localhost:6379/0

# Fernet Key (incolla la chiave generata al PASSO 6)
FERNET_KEY=LA_TUA_CHIAVE_QUI

# Application
DEBUG=true
ENVIRONMENT=development
```

**💡 Suggerimento:** Per DATABASE_URL, sostituisci `$(whoami)` con il tuo username Mac, oppure esegui:
```bash
echo "postgresql+asyncpg://$(whoami)@localhost:5432/brx_sync_db"
```

Salva il file (in nano: `Ctrl+X`, poi `Y`, poi `Enter`)

---

## 📋 PASSO 8: Crea Schema Database

Esegui lo schema SQL:

```bash
psql -d brx_sync_db -f schema.sql
```

**✅ Se non ci sono errori, lo schema è creato!**

Verifica:

```bash
psql -d brx_sync_db -c "\dt"
```

Dovresti vedere le tabelle: `user_sync_settings`, `user_inventory_items`, `sync_operations`

---

## 📋 PASSO 9: Test Connessioni

Con il virtual environment attivo:

```bash
python test_local.py
```

**✅ Se vedi tutti i check verdi, tutto funziona!**

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

---

## 📋 PASSO 11: Apri il Frontend Test

Nel browser, apri:

```
http://localhost:8000/static/index.html
```

**🎉 Se vedi l'interfaccia, tutto funziona!**

---

## 🐛 Troubleshooting

### Errore: "psql: command not found"
```bash
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
# Aggiungi questa riga al tuo ~/.zshrc per renderla permanente
```

### Errore: "database does not exist"
```bash
createdb brx_sync_db
```

### Errore: "connection refused" (Redis)
```bash
brew services restart redis
```

### Errore: "FERNET_KEY not configured"
Verifica che nel `.env` ci sia la chiave Fernet generata al PASSO 6

### Errore: "DATABASE_URL must use asyncpg driver"
Assicurati che DATABASE_URL inizi con `postgresql+asyncpg://`

---

## ✅ Checklist Finale

- [ ] PostgreSQL installato e avviato
- [ ] Redis installato e avviato  
- [ ] Database `brx_sync_db` creato
- [ ] Schema SQL eseguito
- [ ] Virtual environment creato e attivato
- [ ] Dipendenze Python installate
- [ ] File `.env` configurato con Fernet key
- [ ] Test connessioni passati
- [ ] Server avviato su porta 8000
- [ ] Frontend accessibile

---

## 🚀 Prossimi Passi

1. Configura un utente di test nel frontend
2. Inserisci il token CardTrader
3. Avvia la sincronizzazione
4. Verifica l'inventario

**Buon test! 🎉**
