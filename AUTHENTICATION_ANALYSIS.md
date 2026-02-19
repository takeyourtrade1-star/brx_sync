# Analisi Autenticazione - BRX Sync Microservice

## Obiettivo
Integrare autenticazione RS256 JWT nel microservizio brx_sync, verificando i token emessi dal servizio di autenticazione senza accesso al database degli utenti (Zero Trust Architecture).

---

## 📋 ANALISI SISTEMA AUTENTICAZIONE ESISTENTE

### Architettura RS256

**Algoritmo**: RS256 (RSA con SHA-256)
- **Chiave Privata**: Usata dal servizio auth per FIRMARE i token
- **Chiave Pubblica**: Usata dai microservizi per VERIFICARE i token
- **Vantaggio**: Solo il servizio auth può creare token validi, i microservizi possono solo verificare

### Struttura Token JWT

#### Access Token (15 minuti)
```json
{
  "sub": "user_id (UUID)",
  "email": "user@example.com",
  "security_stamp": "uuid",
  "mfa_verified": true,
  "type": "access",
  "exp": 1234567890,
  "iat": 1234567800
}
```

#### Refresh Token (30 giorni)
```json
{
  "sub": "user_id (UUID)",
  "security_stamp": "uuid",
  "type": "refresh",
  "exp": 1234567890,
  "iat": 1234567800
}
```

### Storage Chiavi

**AWS SSM Parameter Store**:
- `JWT_PRIVATE_KEY_SSM_PATH`: `/prod/ebartex/jwt_private_key` (SecureString)
- `JWT_PUBLIC_KEY_SSM_PATH`: `/prod/ebartex/jwt_public_key` (String)
- **Fallback**: Variabili d'ambiente `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY`

### Verifica Token (Best Practices)

1. **Verifica Firma**: Usa public key per verificare che il token sia stato firmato dal servizio auth
2. **Verifica Expiration**: Controlla che `exp` non sia scaduto
3. **Verifica Tipo**: Controlla che `type == "access"` (non accettare refresh token)
4. **Verifica MFA**: Controlla che `mfa_verified == true`
5. **Verifica Security Stamp**: Opzionale (richiede DB access, ma aumenta sicurezza)

---

## 🔐 IMPLEMENTAZIONE PER BRX_SYNC

### 1. Configurazione

**File**: `app/core/config.py`

Aggiungere:
```python
JWT_PUBLIC_KEY_SSM_PATH: Optional[str] = Field(
    default="/prod/ebartex/jwt_public_key",
    description="SSM path for JWT public key (PEM format)"
)
JWT_PUBLIC_KEY: Optional[str] = Field(
    default=None,
    description="JWT public key (PEM format) - fallback to env"
)
JWT_ALGORITHM: str = Field(default="RS256", description="JWT signing algorithm")
```

### 2. Dependency Autenticazione

**File**: `app/api/dependencies.py` (NUOVO)

Creare dependency per verificare JWT:
- Verifica firma con public key
- Verifica expiration
- Verifica tipo token
- Verifica MFA
- Estrae `user_id` dal payload

### 3. Protezione Endpoint

**Tutti gli endpoint devono richiedere autenticazione**:
- `POST /api/v1/sync/start/{user_id}` → Verifica che `user_id` nel token corrisponda
- `GET /api/v1/sync/status/{user_id}` → Verifica user_id
- `GET /api/v1/sync/inventory/{user_id}` → Verifica user_id
- `PUT /api/v1/sync/inventory/{user_id}/item/{item_id}` → Verifica user_id
- `DELETE /api/v1/sync/inventory/{user_id}/item/{item_id}` → Verifica user_id
- `POST /api/v1/sync/purchase/{user_id}/item/{item_id}` → Verifica user_id
- `POST /api/v1/sync/webhook/user/{user_id}` → Verifica user_id (o HMAC signature)
- `POST /api/v1/sync/sync-from-cardtrader/{user_id}` → Verifica user_id

**Eccezioni** (endpoint pubblici):
- `GET /health/*` → Nessuna autenticazione
- `GET /metrics` → Nessuna autenticazione (o autenticazione separata)

### 4. Gestione Refresh Token

**Il microservizio brx_sync NON gestisce refresh token direttamente.**

**Flusso**:
1. Client riceve `401 Unauthorized` (token scaduto)
2. Client chiama `/api/auth/refresh` sul servizio auth
3. Servizio auth verifica refresh token e emette nuovo access token
4. Client usa nuovo access token per chiamare brx_sync

**Implementazione**:
- Quando token scaduto → `401` con header `WWW-Authenticate: Bearer`
- Client deve gestire refresh autonomamente

### 5. Security Stamp (Opzionale)

**Per massima sicurezza**, verificare `security_stamp`:
- Richiede query al DB auth (non ideale per Zero Trust)
- Alternativa: Cache Redis con TTL breve
- **Raccomandazione**: Implementare solo se necessario, altrimenti skip

---

## 🛡️ BEST PRACTICES IMPLEMENTATE

### 1. Zero Trust Architecture
- ✅ Nessun accesso al DB auth
- ✅ Solo verifica crittografica
- ✅ Public key in sola lettura

### 2. Token Validation Completa
- ✅ Verifica firma RS256
- ✅ Verifica expiration
- ✅ Verifica tipo token
- ✅ Verifica MFA
- ✅ Estrazione user_id sicura

### 3. Error Handling
- ✅ `401 Unauthorized` per token invalido/scaduto
- ✅ `403 Forbidden` per token valido ma senza permessi
- ✅ `503 Service Unavailable` per errori configurazione

### 4. Performance
- ✅ Public key caricata una volta all'avvio
- ✅ Cache public key in memoria
- ✅ Verifica JWT veloce (solo crittografia)

---

## 📝 ENDPOINT DA PROTEGGERE

### Endpoint Autenticati (Richiedono JWT)

| Endpoint | Metodo | Autenticazione | Note |
|----------|--------|----------------|------|
| `/api/v1/sync/start/{user_id}` | POST | ✅ JWT | Verifica `user_id` nel token |
| `/api/v1/sync/status/{user_id}` | GET | ✅ JWT | Verifica `user_id` |
| `/api/v1/sync/task/{task_id}` | GET | ✅ JWT | Verifica ownership task |
| `/api/v1/sync/inventory/{user_id}` | GET | ✅ JWT | Verifica `user_id` |
| `/api/v1/sync/inventory/{user_id}/item/{item_id}` | PUT | ✅ JWT | Verifica `user_id` |
| `/api/v1/sync/inventory/{user_id}/item/{item_id}` | DELETE | ✅ JWT | Verifica `user_id` |
| `/api/v1/sync/purchase/{user_id}/item/{item_id}` | POST | ✅ JWT | Verifica `user_id` |
| `/api/v1/sync/webhook/user/{user_id}` | POST | ✅ HMAC | Verifica signature webhook |
| `/api/v1/sync/sync-from-cardtrader/{user_id}` | POST | ✅ JWT | Verifica `user_id` |
| `/api/v1/sync/progress/{user_id}` | GET | ✅ JWT | Verifica `user_id` |
| `/api/v1/sync/webhook-url/{user_id}` | GET | ✅ JWT | Verifica `user_id` |

### Endpoint Pubblici (Nessuna Autenticazione)

| Endpoint | Metodo | Note |
|----------|--------|------|
| `/health/live` | GET | Health check |
| `/health/ready` | GET | Readiness check |
| `/metrics` | GET | Prometheus metrics (opzionalmente protetto) |

---

## 🔄 FLUSSO AUTENTICAZIONE

### 1. Client Ottiene Token
```
Client → Auth Service: POST /api/auth/login
Auth Service → Client: { access_token, refresh_token }
```

### 2. Client Usa Token
```
Client → BRX Sync: GET /api/v1/sync/inventory/{user_id}
Headers: Authorization: Bearer <access_token>
BRX Sync → Verifica JWT → Estrae user_id → Verifica match con {user_id}
BRX Sync → Client: 200 OK + data
```

### 3. Token Scaduto
```
Client → BRX Sync: GET /api/v1/sync/inventory/{user_id}
Headers: Authorization: Bearer <expired_token>
BRX Sync → Client: 401 Unauthorized
Client → Auth Service: POST /api/auth/refresh
Auth Service → Client: { access_token, refresh_token }
Client → BRX Sync: GET /api/v1/sync/inventory/{user_id} (con nuovo token)
```

---

## ⚠️ CONSIDERAZIONI SICUREZZA

### 1. User ID Verification
**CRITICO**: Verificare che `user_id` nel token corrisponda a `user_id` nell'URL.

**Esempio**:
```python
token_user_id = payload["sub"]
if token_user_id != user_id:
    raise HTTPException(403, "User ID mismatch")
```

### 2. Webhook Authentication
I webhook da CardTrader usano HMAC signature, non JWT.
- Verificare signature HMAC con `webhook_secret`
- Non richiedere JWT per webhook endpoint

### 3. Rate Limiting
- Rate limiting già implementato per-user
- Usare `user_id` dal token per rate limiting

### 4. Logging
- Non loggare token completi (solo hash o user_id)
- Loggare tentativi di accesso non autorizzati

---

## 📦 DIPENDENZE RICHIESTE

Aggiungere a `requirements.txt`:
```
PyJWT>=2.8.0
cryptography>=41.0.0  # Per RS256
```

---

## ✅ CHECKLIST IMPLEMENTAZIONE

- [ ] Aggiungere configurazione JWT in `config.py`
- [ ] Creare `app/api/dependencies.py` con `get_current_user_id`
- [ ] Aggiungere dependency a tutti gli endpoint protetti
- [ ] Verificare user_id match tra token e URL
- [ ] Gestire errori 401/403 correttamente
- [ ] Testare con token validi/invalidi/scaduti
- [ ] Documentare flusso refresh token
- [ ] Aggiornare frontend per gestire 401 e refresh

---

## 🎯 CONCLUSIONE

Il sistema di autenticazione RS256 è **robusto e sicuro**:
- ✅ Zero Trust (solo verifica crittografica)
- ✅ Scalabile (nessun DB access)
- ✅ Standard (JWT RS256)
- ✅ Compatibile con refresh token

**Implementazione**: Seguire il pattern di `search_engine/app/api/dependencies.py` adattandolo per brx_sync.
