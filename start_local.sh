#!/bin/bash

# Script per avviare il microservizio in locale per test

echo "🚀 BRX Sync - Avvio Locale"
echo "=========================="
echo ""

# Verifica variabili d'ambiente
if [ ! -f .env ]; then
    echo "⚠️  File .env non trovato!"
    echo "📝 Creando .env da .env.example..."
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "✅ File .env creato. Configura le variabili d'ambiente!"
        echo ""
        echo "⚠️  IMPORTANTE: Modifica .env con le tue credenziali:"
        echo "   - DATABASE_URL (PostgreSQL)"
        echo "   - MYSQL_* (per blueprint mapping)"
        echo "   - REDIS_URL"
        echo "   - FERNET_KEY"
        echo ""
        read -p "Premi INVIO quando hai configurato .env..."
    else
        echo "❌ File .env.example non trovato!"
        exit 1
    fi
fi

# L'app usa pydantic-settings per leggere .env senza valutarlo come codice shell.

echo "📦 Verifica dipendenze Python..."
if [ ! -d "venv" ]; then
    echo "🔧 Creazione virtual environment..."
    python3 -m venv venv
fi

echo "📥 Installazione dipendenze..."
source venv/bin/activate
python -m pip install -q --requirement requirements.txt

echo ""
echo "🧪 Esegui test connessioni..."
python test_local.py

echo ""
echo "🌐 Avvio FastAPI server..."
echo "   Frontend test: http://localhost:8000/static/index.html"
echo "   API Docs: http://localhost:8000/docs"
echo "   Health: http://localhost:8000/health/ready"
echo ""
echo "⚠️  IMPORTANTE: Per processare i task Celery, avvia il worker in un altro terminale:"
echo "   cd $(pwd)"
echo "   source venv/bin/activate"
echo "   celery -A app.tasks.celery_app worker --loglevel=info -Q bulk-sync,high-priority,default"
echo ""
echo "⚠️  Per testare CardTrader API, esporta:"
echo "   export CARDTRADER_TOKEN='your_token'"
echo ""

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
