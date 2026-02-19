# CardTrader API - Campi Modificabili

## 📋 Riepilogo

Secondo la documentazione ufficiale CardTrader V2 API, quando aggiorni un Product con `PUT /products/:id` o `POST /products/bulk_update`, puoi modificare:

## ✅ Campi Top-Level Modificabili

Questi campi vanno **direttamente nell'oggetto principale**, NON dentro `properties`:

| Campo | Tipo | Obbligatorio | Descrizione |
|-------|------|--------------|-------------|
| `id` | integer | ✅ **Sì** | ID del Product da modificare |
| `price` | float | ❌ No | Prezzo del prodotto nella tua valuta |
| `quantity` | integer | ❌ No | Quantità disponibile |
| `description` | string | ❌ No | Descrizione visibile a tutti |
| `user_data_field` | string | ❌ No | Campo testo per metadata (es. posizione magazzino) |
| `graded` | boolean | ❌ No | Se il prodotto è gradato (true/false) |

**⚠️ IMPORTANTE:** `graded` è un campo **top-level**, NON va dentro `properties`!

## ✅ Proprietà Modificabili (dentro `properties` object)

Le proprietà modificabili sono **solo quelle presenti nell'`editable_properties` del Blueprint**.

Secondo la documentazione (linea 923):
> "The possible properties are those in the editable_properties object of the product Blueprint."

### Proprietà Comuni per Magic: The Gathering

Queste sono le proprietà **tipicamente editabili** per prodotti MTG:

| Proprietà | Tipo | Valori Possibili | Esempio |
|-----------|------|------------------|---------|
| `condition` | string | "Mint", "Near Mint", "Slightly Played", "Moderately Played", "Played", "Heavily Played", "Poor" | `"Near Mint"` |
| `mtg_language` | string | Codice lingua (es. "en", "it", "de", "fr", "es", "jp") | `"en"` |
| `mtg_foil` | boolean | `true` o `false` | `false` |
| `signed` | boolean | `true` o `false` | `false` |
| `altered` | boolean | `true` o `false` | `false` |

**⚠️ NOTA:** Le proprietà editabili possono variare per blueprint. Per sapere esattamente quali proprietà sono editabili per un blueprint specifico, devi consultare l'`editable_properties` di quel blueprint.

## ❌ Proprietà Read-Only (NON Modificabili)

Queste proprietà sono **derivate dal Blueprint** e **NON possono essere modificate**:

| Proprietà | Motivo |
|-----------|--------|
| `mtg_card_colors` | Derivato dal blueprint |
| `collector_number` | Derivato dal blueprint |
| `tournament_legal` | Derivato dal blueprint |
| `cmc` (Converted Mana Cost) | Derivato dal blueprint |
| `mtg_rarity` | Derivato dal blueprint |

Se provi a modificarle, CardTrader le ignorerà e restituirà un warning:
```
"Read only property [nome_proprietà] has been ignored"
```

## 📝 Formato Richiesta Esempio

### PUT /products/:id (singolo prodotto)

```json
{
  "id": 392763065,
  "price": 337.57,
  "quantity": 1,
  "graded": false,
  "properties": {
    "condition": "Near Mint",
    "mtg_language": "en",
    "mtg_foil": false,
    "signed": false,
    "altered": false
  }
}
```

### POST /products/bulk_update (più prodotti)

```json
{
  "products": [
    {
      "id": 392763065,
      "price": 337.57,
      "quantity": 1,
      "graded": false,
      "properties": {
        "condition": "Near Mint",
        "mtg_language": "en",
        "mtg_foil": false
      }
    }
  ]
}
```

## 🔍 Come Verificare le Proprietà Editabili

Per sapere esattamente quali proprietà sono editabili per un blueprint specifico:

1. **Consulta il Blueprint:**
   ```
   GET /blueprints/:id
   ```
   Cerca il campo `editable_properties` nella risposta.

2. **Esempio risposta:**
   ```json
   {
     "id": 310284,
     "name": "Black Lotus",
     "editable_properties": [
       {
         "name": "condition",
         "type": "string",
         "possible_values": ["Mint", "Near Mint", "Slightly Played", ...]
       },
       {
         "name": "mtg_language",
         "type": "string",
         "possible_values": ["en", "it", "de", "fr", ...]
       },
       {
         "name": "mtg_foil",
         "type": "boolean",
         "possible_values": [true, false]
       },
       {
         "name": "signed",
         "type": "boolean",
         "possible_values": [true, false]
       },
       {
         "name": "altered",
         "type": "boolean",
         "possible_values": [true, false]
       }
     ]
   }
   ```

## ⚠️ Warning da CardTrader

Se invii proprietà read-only, CardTrader restituirà un warning ma **completerà comunque l'operazione**:

```json
{
  "result": "warning",
  "warnings": {
    "properties": {
      "mtg_card_colors": ["Read only property mtg_card_colors has been ignored"],
      "collector_number": ["Read only property collector_number has been ignored"],
      "mtg_rarity": ["Read only property mtg_rarity has been ignored"]
    }
  }
}
```

## ✅ Best Practices

1. **Non inviare proprietà read-only** - Filtra prima di inviare per evitare warning
2. **Verifica `editable_properties`** - Per blueprint specifici, consulta sempre l'`editable_properties`
3. **Usa `error_mode: "strict"`** - Se vuoi che l'API fallisca invece di restituire warning
4. **`graded` è top-level** - Non metterlo dentro `properties`

## 📚 Riferimenti

- Documentazione CardTrader: `doc_card_trader.txt`
- Linea 923: Spiegazione `editable_properties`
- Linea 976-979: Campi modificabili in `PUT /products/:id`
- Linea 1155-1170: Campi modificabili in `POST /products/bulk_update`
