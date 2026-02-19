# 🔔 Guida Setup Webhook per Utente

## 📋 Panoramica

Ogni utente deve configurare il proprio endpoint webhook su CardTrader. Questo permette a CardTrader di inviare notifiche quando vengono creati, modificati o eliminati ordini o prodotti.

## 🚀 Setup Rapido

### Step 1: Configura Utente nel Sistema

```bash
POST /api/v1/sync/setup-test-user
{
  "user_id": "your-uuid-here",
  "cardtrader_token": "your-cardtrader-token"
}
```

Questo:
- ✅ Cripta e salva il token CardTrader
- ✅ Recupera automaticamente il `shared_secret` da CardTrader
- ✅ Configura le impostazioni di sincronizzazione

### Step 2: Ottieni l'URL Webhook

```bash
GET /api/v1/sync/webhook-url/{user_id}
```

**Esempio risposta:**
```json
{
  "user_id": "13691be3-8afb-428c-b054-b86ee5a0eae6",
  "webhook_url": "https://your-domain.com/api/v1/sync/webhook/user/13691be3-8afb-428c-b054-b86ee5a0eae6",
  "instructions": {
    "step_1": "Go to https://www.cardtrader.com/it/full_api_app",
    "step_2": "Copy the webhook URL above",
    "step_3": "Paste it in the 'Indirizzo del tuo endpoint webhook' field",
    "step_4": "Click 'Salva l'endpoint del Webhook'"
  },
  "webhook_secret_configured": true
}
```

### Step 3: Configura su CardTrader

1. **Vai su CardTrader**: https://www.cardtrader.com/it/full_api_app
2. **Copia l'URL webhook** ottenuto dall'endpoint sopra
3. **Incolla** l'URL nel campo **"Indirizzo del tuo endpoint webhook"**
4. **Clicca** su **"Salva l'endpoint del Webhook"**

## 📸 Screenshot CardTrader

Sulla pagina CardTrader vedrai:
- **JWT Token**: Token per le chiamate API
- **Webhook**: Campo per inserire l'endpoint

**Testo sulla pagina CardTrader:**
> "Se specifichi un endpoint, manderemo una notifica al tuo endpoint ogni volta che un Prodotto o un Ordine vengono creati, modificati o eliminati. L'endpoint non viene chiamato se le operazioni in questione vengono effettuate da te stesso tramite l'utilizzo delle API."

## ✅ Verifica Configurazione

Dopo aver configurato il webhook:

1. **Crea un ordine di test** su CardTrader
2. **Verifica i log** del server per vedere il webhook ricevuto
3. **Controlla** che le quantità siano state aggiornate correttamente

## 🔍 Endpoint Webhook

### Endpoint Principale (per Utente)

```
POST /api/v1/sync/webhook/user/{user_id}
```

**Caratteristiche:**
- ✅ `user_id` è nell'URL (chiaro e diretto)
- ✅ Validazione firma con `shared_secret` dell'utente
- ✅ Processing asincrono (< 100ms di risposta)
- ✅ Isolamento per utente (ogni utente ha il suo endpoint)

### Endpoint Legacy (Backward Compatibility)

```
POST /api/v1/sync/webhook/{webhook_id}
```

**Caratteristiche:**
- ⚠️ Estrae `user_id` dal payload (meno sicuro)
- ✅ Mantenuto per compatibilità
- ⚠️ Non raccomandato per nuovi setup

## 🛡️ Sicurezza

### Validazione Firma

Ogni webhook viene validato usando:
- **Header**: `Signature` (HMAC-SHA256)
- **Secret**: `shared_secret` dell'utente (da CardTrader `/info`)
- **Body**: Payload JSON completo

Se la validazione fallisce:
- ⚠️ Viene loggato un warning
- ✅ Il webhook viene comunque processato (per evitare perdite di dati)
- 🔒 In produzione, considera di rifiutare webhook non validati

## 📝 Note Importanti

1. **Endpoint per Utente**: Ogni utente ha il suo endpoint specifico
2. **Configurazione Manuale**: L'utente deve configurare l'endpoint su CardTrader
3. **No Loop Infiniti**: CardTrader non invia webhook per operazioni fatte via API
4. **Risposta Veloce**: L'endpoint deve rispondere in < 100ms
5. **Processing Asincrono**: Il processing avviene in background tramite Celery

## 🧪 Testing Locale

Per testare in locale, usa `ngrok`:

```bash
# 1. Avvia ngrok
ngrok http 8000

# 2. Ottieni l'URL ngrok (es: https://abc123.ngrok.io)

# 3. Costruisci l'URL webhook completo
# https://abc123.ngrok.io/api/v1/sync/webhook/user/{user_id}

# 4. Configura questo URL su CardTrader

# 5. Crea un ordine di test e verifica i log
```

## 🔗 Link Utili

- **CardTrader API Settings**: https://www.cardtrader.com/it/full_api_app
- **Documentazione Webhook**: `doc_card_trader.txt#L2069`
- **Endpoint API**: `GET /api/v1/sync/webhook-url/{user_id}`
