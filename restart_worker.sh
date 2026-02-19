#!/bin/bash

# Script per riavviare rapidamente il worker Celery

cd "$(dirname "$0")"

echo "🔄 Riavvio Worker Celery"
echo "========================"
echo ""

# Kill worker esistenti
echo "🛑 Fermando worker esistenti..."
pkill -f "celery.*celery_app.*worker" 2>/dev/null

if [ $? -eq 0 ]; then
    echo "✅ Worker fermati"
else
    echo "ℹ️  Nessun worker trovato in esecuzione"
fi

# Attendi 2 secondi per permettere la chiusura pulita
echo "⏳ Attesa 2 secondi..."
sleep 2

# Verifica che Redis sia attivo
echo "🔍 Verifica Redis..."
if command -v redis-cli &> /dev/null; then
    if redis-cli ping &> /dev/null; then
        echo "✅ Redis attivo"
    else
        echo "⚠️  Redis non raggiungibile. Assicurati che sia avviato:"
        echo "   brew services start redis"
        echo ""
        read -p "Vuoi continuare comunque? (y/n) " -n 1 -r
        echo ""
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
else
    echo "⚠️  redis-cli non trovato. Assicurati che Redis sia installato e avviato."
fi

# Attiva virtual environment
if [ ! -d "venv" ]; then
    echo "❌ Virtual environment non trovato!"
    echo "   Esegui prima: python3 -m venv venv"
    exit 1
fi

echo "🐍 Attivazione virtual environment..."
source venv/bin/activate

# Verifica dipendenze
echo "📦 Verifica dipendenze..."
if ! python -c "import celery" &> /dev/null; then
    echo "⚠️  Celery non trovato. Installazione dipendenze..."
    pip install -q -r requirements.txt
fi

# Crea directory logs se non esiste
mkdir -p logs

# Riavvia worker
echo ""
echo "🚀 Avvio worker Celery..."
echo "   Queue: bulk-sync, high-priority, default"
echo "   Log level: info"
echo ""
echo "💡 Per fermare il worker, premi Ctrl+C"
echo ""

celery -A app.tasks.celery_app worker \
    --loglevel=info \
    -Q bulk-sync,high-priority,default \
    --logfile=logs/celery_worker.log \
    --pidfile=logs/celery_worker.pid
