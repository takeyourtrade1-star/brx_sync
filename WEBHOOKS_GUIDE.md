# 🔄 Guida Sincronizzazione Bidirezionale con Webhook

## 📋 Panoramica

Il sistema di sincronizzazione bidirezionale consente di mantenere l'inventario sincronizzato tra Ebartex e CardTrader in entrambe le direzioni:

1. **Ebartex → CardTrader**: Modifiche fatte tramite la nostra API vengono sincronizzate su CardTrader
2. **CardTrader → Ebartex**: Modifiche fatte su CardTrader (ordini, modifiche dirette) vengono sincronizzate nel nostro database

## 🔔 Webhook di CardTrader

### Endpoint Webhook

```
POST /api/v1/sync/webhook/{webhook_id}
```

CardTrader invia webhook quando:
- Un ordine viene creato (`order.create`)
- Un ordine viene modificato (`order.update`)
- Un ordine viene eliminato (`order.destroy`)

### Eventi Gestiti

#### 1. `order.create` (Ordine Pagato)

Quando un ordine viene creato e pagato:
- ✅ Le quantità dei prodotti venduti vengono **decrementate** automaticamente
- ✅ Il database locale viene aggiornato in tempo reale
- ✅ Processing asincrono (< 100ms di risposta)

**Esempio:**
```json
{
  "cause": "order.create",
  "data": {
    "id": 733733,
    "state": "paid",
    "order_items": [
      {
        "product_id": 392763065,
        "quantity": 2
      }
    ]
  }
}
```

#### 2. `order.update` (Cambio Stato)

Quando lo stato di un ordine cambia:
- ✅ Se l'ordine viene **cancellato** → quantità ripristinate
- ✅ Se l'ordine passa da `paid` a altro stato → quantità ripristinate
- ✅ Altri cambi di stato vengono loggati ma non modificano quantità

**Stati gestiti:**
- `canceled`: Quantità ripristinate
- `request_for_cancel`: Quantità ripristinate
- Altri stati: Solo logging

#### 3. `order.destroy` (Ordine Eliminato)

Quando un ordine viene eliminato:
- ✅ Le quantità vengono **ripristinate** completamente
- ✅ Il database locale viene aggiornato

### Validazione Firma

I webhook vengono validati usando HMAC-SHA256:
- Header: `Signature`
- Secret: `shared_secret` dal CardTrader `/info` endpoint
- Se la validazione fallisce, viene loggato un warning ma il webhook viene comunque processato (per evitare perdite di dati)

## 🔄 Sincronizzazione Periodica

### Endpoint Manuale

```
POST /api/v1/sync/sync-from-cardtrader/{user_id}?blueprint_id={optional}
```

Sincronizza manualmente i prodotti da CardTrader al database locale.

**Quando usarlo:**
- Dopo modifiche dirette su CardTrader UI
- Per verificare che tutto sia sincronizzato
- Per sincronizzare un prodotto specifico (`blueprint_id`)

**Esempio:**
```bash
curl -X POST "http://localhost:8000/api/v1/sync/sync-from-cardtrader/13691be3-8afb-428c-b054-b86ee5a0eae6?blueprint_id=310284"
```

**Risposta:**
```json
{
  "status": "accepted",
  "task_id": "abc123-def456-...",
  "user_id": "13691be3-8afb-428c-b054-b86ee5a0eae6",
  "blueprint_id": 310284,
  "message": "Sync from CardTrader queued"
}
```

### Cosa Viene Sincronizzato

Durante la sincronizzazione periodica, vengono aggiornati:
- ✅ `quantity` - Quantità disponibile
- ✅ `price_cents` - Prezzo in centesimi
- ✅ `description` - Descrizione prodotto
- ✅ `user_data_field` - Campo metadata personalizzato
- ✅ `graded` - Se il prodotto è gradato
- ✅ `properties` - Proprietà prodotto (condition, signed, altered, etc.)

## 🛡️ Prevenzione Loop Infiniti

Il sistema è progettato per evitare loop infiniti:

1. **Webhook Processing**: Quando processiamo un webhook, aggiorniamo solo il database locale, **non** inviamo modifiche a CardTrader
2. **Sync Periodico**: Quando sincronizziamo da CardTrader, aggiorniamo solo il database locale
3. **Modifiche Manuali**: Quando modifichiamo tramite API, inviamo a CardTrader ma **non** triggeriamo sync periodico

**Flusso:**
```
CardTrader Order → Webhook → Update Local DB (NO sync to CardTrader)
Our API Update → Update Local DB → Sync to CardTrader
CardTrader Direct Edit → Periodic Sync → Update Local DB (NO sync to CardTrader)
```

## 📊 Logging e Monitoraggio

### Log Webhook

I webhook vengono loggati con:
- `webhook_id`: UUID del webhook
- `cause`: Tipo di evento (order.create, order.update, order.destroy)
- `order_id`: ID dell'ordine
- `items_processed`: Numero di prodotti processati
- `processing_time_ms`: Tempo di processing

### Log Sync Periodico

La sincronizzazione periodica logga:
- `updated`: Numero di prodotti aggiornati
- `created`: Numero di prodotti creati
- `errors`: Lista di errori (se presenti)

## 🔧 Configurazione

### 1. Setup Utente

Prima di tutto, configura l'utente nel sistema:

```bash
POST /api/v1/sync/setup-test-user
{
  "user_id": "your-uuid",
  "cardtrader_token": "your-token"
}
```

Il sistema chiama automaticamente `/info` per ottenere il `shared_secret`.

### 2. Ottenere l'URL Webhook

Ottieni l'URL webhook specifico per il tuo utente:

```bash
GET /api/v1/sync/webhook-url/{user_id}
```

**Risposta:**
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

### 3. Configurare Webhook su CardTrader

**IMPORTANTE**: Ogni utente deve configurare il proprio endpoint webhook su CardTrader:

1. Vai su **https://www.cardtrader.com/it/full_api_app**
2. Copia l'URL webhook ottenuto dall'endpoint `/webhook-url/{user_id}`
3. Incolla l'URL nel campo **"Indirizzo del tuo endpoint webhook"**
4. Clicca su **"Salva l'endpoint del Webhook"**

**Nota**: CardTrader invierà notifiche a questo endpoint quando:
- Un **Prodotto** viene creato, modificato o eliminato
- Un **Ordine** viene creato, modificato o eliminato

**IMPORTANTE**: L'endpoint **NON** viene chiamato se le operazioni vengono effettuate tramite le API (per evitare loop infiniti).

## 🧪 Testing

### Test Webhook Locale

Per testare i webhook in locale, puoi usare `ngrok` o simili:

```bash
# 1. Avvia ngrok
ngrok http 8000

# 2. Configura l'URL su CardTrader
# https://your-ngrok-url.ngrok.io/api/v1/sync/webhook/{webhook_id}

# 3. Crea un ordine su CardTrader e verifica i log
```

### Test Manuale

```bash
# Simula webhook order.create
curl -X POST "http://localhost:8000/api/v1/sync/webhook/test-123" \
  -H "Content-Type: application/json" \
  -H "Signature: test-signature" \
  -d '{
    "id": "test-123",
    "cause": "order.create",
    "data": {
      "id": 12345,
      "state": "paid",
      "seller": {"id": "your-user-id"},
      "order_items": [
        {
          "product_id": "392763065",
          "quantity": 1
        }
      ]
    }
  }'
```

## ⚠️ Note Importanti

1. **Risposta Veloce**: L'endpoint webhook deve rispondere in < 100ms. Il processing avviene asincrono.

2. **Idempotenza**: I webhook vengono processati in modo idempotente. Se lo stesso webhook arriva più volte, viene processato solo una volta.

3. **Error Handling**: Se il processing fallisce, il webhook viene riprovato automaticamente da CardTrader (se configurato).

4. **Quantità Negative**: Le quantità non possono andare sotto 0. Se un ordine richiede più quantità di quella disponibile, viene loggato un warning.

5. **Prodotti Non Trovati**: Se un prodotto venduto su CardTrader non esiste nel database locale, viene loggato un warning ma il webhook viene comunque processato.

## 📚 Riferimenti

- [Documentazione CardTrader Webhooks](doc_card_trader.txt#L2069)
- [API Endpoints](app/api/v1/routes/sync.py)
- [Webhook Processor](app/services/webhook_processor.py)
- [Periodic Sync Tasks](app/tasks/periodic_sync.py)
