#!/bin/bash

# Script di setup automatico per macOS
# Esegue i passi principali di configurazione

set -euo pipefail

echo "🍎 BRX Sync - Setup Automatico macOS"
echo "======================================"
echo ""

# Colori per output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Funzione per check
check_command() {
    if command -v "$1" &> /dev/null; then
        echo -e "${GREEN}✓${NC} $1 installato"
        return 0
    else
        echo -e "${RED}✗${NC} $1 non trovato"
        return 1
    fi
}

# PASSO 1: Verifica Homebrew
echo "📦 Verifica Homebrew..."
if ! check_command brew; then
    echo -e "${RED}✗${NC} Homebrew non trovato."
    echo "Installa Homebrew separatamente seguendo la documentazione ufficiale e verifica lo script prima di eseguirlo."
    exit 1
fi

# PASSO 2: Verifica PostgreSQL
echo ""
echo "🐘 Verifica PostgreSQL..."
if ! check_command psql; then
    echo -e "${YELLOW}⚠️  PostgreSQL non trovato. Installazione...${NC}"
    brew install postgresql@16
    brew services start postgresql@16
    sleep 2
fi

# Verifica se il database esiste
if psql -lqt | cut -d \| -f 1 | grep -qw brx_sync_db; then
    echo -e "${GREEN}✓${NC} Database brx_sync_db esiste"
else
    echo -e "${YELLOW}⚠️  Creazione database brx_sync_db...${NC}"
    createdb brx_sync_db || echo -e "${RED}✗${NC} Errore creazione database (potrebbe esistere già)"
fi

# PASSO 3: Verifica Redis
echo ""
echo "🔴 Verifica Redis..."
if ! check_command redis-cli; then
    echo -e "${YELLOW}⚠️  Redis non trovato. Installazione...${NC}"
    brew install redis
    brew services start redis
    sleep 2
fi

# Test Redis
if redis-cli ping &> /dev/null; then
    echo -e "${GREEN}✓${NC} Redis funziona"
else
    echo -e "${YELLOW}⚠️  Avvio Redis...${NC}"
    brew services start redis
    sleep 2
fi

# PASSO 4: Verifica Python
echo ""
echo "🐍 Verifica Python..."
if ! check_command python3; then
    echo -e "${RED}✗${NC} Python3 non trovato. Installa Python 3.12+"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1,2)
echo -e "${GREEN}✓${NC} Python $PYTHON_VERSION trovato"

# PASSO 5: Setup Virtual Environment
echo ""
echo "📦 Setup Virtual Environment..."
if [ ! -d "venv" ]; then
    echo "Creazione venv..."
    python3 -m venv venv
fi

echo "Attivazione venv..."
source venv/bin/activate

# PASSO 6: Installa Dipendenze
echo ""
echo "📥 Installazione dipendenze..."
python -m pip install --upgrade pip==26.2 --quiet
python -m pip install --requirement requirements.txt --quiet
echo -e "${GREEN}✓${NC} Dipendenze installate"

# PASSO 7: Genera Fernet Key
echo ""
echo "🔐 Generazione chiave Fernet..."
FERNET_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
echo -e "${GREEN}✓${NC} Chiave generata"

# PASSO 8: Setup .env
echo ""
echo "📝 Setup file .env..."
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo -e "${GREEN}✓${NC} File .env creato da .env.example"
    else
        echo -e "${RED}✗${NC} .env.example non trovato; setup interrotto senza creare credenziali predefinite"
        exit 1
    fi
else
    echo -e "${YELLOW}⚠️  File .env già esistente${NC}"
    # Aggiorna solo FERNET_KEY se non presente
    if ! grep -q "FERNET_KEY=" .env; then
        echo "FERNET_KEY=${FERNET_KEY}" >> .env
        echo -e "${GREEN}✓${NC} FERNET_KEY aggiunto a .env"
    fi
fi

# PASSO 9: Crea Schema Database
echo ""
echo "🗄️  Creazione schema database..."
if [ -f schema.sql ]; then
    psql -d brx_sync_db -f schema.sql > /dev/null 2>&1 || echo -e "${YELLOW}⚠️  Schema potrebbe essere già creato${NC}"
    echo -e "${GREEN}✓${NC} Schema verificato"
else
    echo -e "${RED}✗${NC} schema.sql non trovato"
fi

echo ""
echo "======================================"
echo -e "${GREEN}✅ Setup completato!${NC}"
echo "======================================"
echo ""
echo "📋 Prossimi passi:"
echo ""
echo "1. Attiva virtual environment:"
echo "   source venv/bin/activate"
echo ""
echo "2. Testa connessioni:"
echo "   python test_local.py"
echo ""
echo "3. Avvia server:"
echo "   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"
echo ""
echo "4. Apri frontend:"
echo "   http://localhost:8000/static/index.html"
echo ""
echo "💡 La chiave Fernet è già configurata nel .env"
echo ""
