# Sync – Risoluzione problemi (ERR_CONNECTION_REFUSED, Failed to fetch, CORS)

## 1. `sync.ebartex.com` – Impossibile raggiungere il sito / Connessione negata

**ERR_CONNECTION_REFUSED** significa che **nessun servizio sta ascoltando** su quell’indirizzo/porta. Il browser non arriva nemmeno al server, quindi non è un problema CORS.

Da verificare:

1. **Il microservizio Sync è in esecuzione su AWS?**
   - ECS: task/servizio del sync attivo?
   - EC2: container/processo in ascolto sulla porta (es. 8000)?

2. **DNS**
   - `sync.ebartex.com` punta all’ALB o all’IP del server dove gira il sync?

3. **Load balancer / Ingress**
   - C’è un ALB (o Nginx/OpenResty) con un listener per `sync.ebartex.com` che inoltra il traffico al target (porta 8000)?
   - Security group: in ingresso sono consentite le porte 80/443 (e la porta del backend se esposta)?

4. **Target group (se usi ALB)**
   - I target (istanze/ECS) sono “healthy”?
   - La health check punta a `http://...:8000/health` o `/health/live`?

Se una di queste manca, il sync non è raggiungibile e vedi “Connessione negata”. I log del microservizio restano vuoti perché le richieste non arrivano mai.

---

## 2. CORS e “Failed to fetch” (frontend Amplify)

Quando il sync **è** raggiungibile (es. `https://sync.ebartex.com`) ma il frontend su **Amplify** (`https://main.d8ry9s45st8bf.amplifyapp.com`) fa richieste cross-origin, il browser applica CORS. Se l’origine non è consentita, vedi “Failed to fetch” (o errori CORS in console).

### Cosa è stato configurato nel codice

- In **`app/main.py`**:
  - Le origini consentite vengono da `ALLOWED_ORIGINS` (split per `,`).
  - È aggiunta automaticamente l’origine **`https://main.d8ry9s45st8bf.amplifyapp.com`** se non è già in lista (e se non usi `*`).
  - CORS: `allow_credentials=True`, metodi `GET, POST, PUT, DELETE, OPTIONS`, headers consentiti.

- In **`.env.example`** (e da impostare in produzione):
  ```env
  ALLOWED_ORIGINS=https://main.d8ry9s45st8bf.amplifyapp.com
  ```
  Per più domini (es. sito production):
  ```env
  ALLOWED_ORIGINS=https://main.d8ry9s45st8bf.amplifyapp.com,https://www.ebartex.com
  ```

### In produzione (AWS)

- Nel **.env** (o variabili d’ambiente) del sync su AWS imposta:
  ```env
  ALLOWED_ORIGINS=https://main.d8ry9s45st8bf.amplifyapp.com
  ```
- Riavvia il servizio Sync dopo la modifica.

---

## 3. Riepilogo stato domini (da te indicati)

| Dominio                | Stato attuale        | Azione |
|------------------------|----------------------|--------|
| api.ebartex.com       | OK (auth-service)    | -      |
| search.ebartex.com    | OK (Meilisearch)     | -      |
| market.ebartex.com    | 502 Bad Gateway      | Verificare backend market e proxy (Nginx/ALB). |
| **sync.ebartex.com**  | **Connessione negata** | Far partire il sync e esporlo (ALB/Nginx + DNS + SG). Poi impostare CORS come sopra. |

---

## 4. Verifica rapida dopo il deploy del sync

```bash
# Da terminale (il sync deve essere raggiungibile)
curl -s https://sync.ebartex.com/health
# oppure
curl -s https://sync.ebartex.com/
```

Se risponde con JSON (es. `{"status":"alive"}` o `{"service":"brx-sync",...}`), il servizio è raggiungibile. A quel punto, con `ALLOWED_ORIGINS` che include `https://main.d8ry9s45st8bf.amplifyapp.com`, le chiamate dal frontend Amplify non dovrebbero più dare “Failed to fetch” per CORS.
