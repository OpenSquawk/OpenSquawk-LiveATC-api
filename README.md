# OpenSquawk LiveATC API

Backend-API für OpenSquawk/LiveATC-Training.

Dieser Teil ist das FastAPI-Backend für die deterministischen Decision-Flows. Es lädt Flow-YAMLs aus `flows/`, erstellt Trainings-Sessions und nimmt Pilot-Transmissions vom Frontend entgegen.

## Voraussetzungen

- Python 3.12
- Poetry

## Lokal starten

```bash
poetry install
poetry run uvicorn main:app --reload
```

oder

```bash
.venv/bin/uvicorn main:app --reload
```

Standard lokal:

```text
http://127.0.0.1:8000
```

Healthcheck:

```bash
curl http://127.0.0.1:8000/
```

Swagger/OpenAPI:

```text
http://127.0.0.1:8000/docs
```

## Port ändern

Lokal direkt über Uvicorn:

```bash
poetry run uvicorn main:app --reload --host 0.0.0.0 --port 3000
```

Oder per Environment-Variable, passend zum Deploy-Setup:

```bash
PORT=3000 poetry run uvicorn main:app --host 0.0.0.0 --port "$PORT"
```

Im Deployment nutzt `nixpacks.toml`:

```bash
uvicorn main:app --host 0.0.0.0 --port ${PORT:-3000}
```

Wenn der Host also `PORT` setzt, wird dieser Port verwendet. Ohne `PORT` fällt das Deployment auf `3000` zurück.

## Wichtige Environment-Variablen

```bash
FLOWS_DIR=./flows
SESSION_STORE_TYPE=memory
MAX_FLOW_STACK_DEPTH=5
MAX_AUTO_ADVANCE_HOPS=50
READBACK_TIMEOUT_MS=30000
READBACK_SILENCE_MS=40000
LOG_LEVEL=info
```

Meist reicht lokal die Default-Konfiguration.

## Deployment

Das Repo ist für Nixpacks vorbereitet.

Build/Install:

```bash
poetry install --only main --no-interaction --no-ansi
```

Start Command:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port ${PORT:-3000}
```

Bei Railway, Render, Fly, Coolify oder ähnlichen Hosts:

1. Repo verbinden
2. Nixpacks/Python als Builder verwenden
3. `PORT` vom Host setzen lassen oder selbst setzen
4. Start Command aus `nixpacks.toml` verwenden

## Vom Frontend aufrufen

Base URL lokal:

```ts
const API_BASE_URL = "http://127.0.0.1:8000";
```

Runtime-Flows laden:

```ts
const flows = await fetch(`${API_BASE_URL}/api/decision-flows/runtime`)
  .then((res) => res.json());
```

Session starten:

```ts
const session = await fetch(`${API_BASE_URL}/api/radio/session`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ flow_slug: flows.main }),
}).then((res) => res.json());
```

Pilot-Transmission senden:

```ts
const decision = await fetch(
  `${API_BASE_URL}/api/radio/session/${session.session_id}/transmissions`,
  {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      pilot_utterance: "Ready for taxi",
    }),
  },
).then((res) => res.json());
```

Antwort vom Backend:

```ts
console.log(decision.controller_say_rendered);
console.log(decision.next_state_id);
```

## API-Endpunkte

- `GET /` - Healthcheck und geladene Flows
- `GET /api/decision-flows/runtime` - alle Runtime-Flows für Frontend-Bootstrap
- `GET /api/decision-flows/runtime/{slug}` - einzelner Flow
- `GET /api/decision-flows/runtime/{slug}/validate` - Flow validieren
- `POST /api/decision-flows/admin/reload` - Flow-Dateien neu laden
- `POST /api/radio/session` - Session erstellen
- `GET /api/radio/session/{session_id}` - Session abfragen
- `DELETE /api/radio/session/{session_id}` - Session löschen
- `GET /api/radio/sessions` - aktive Sessions listen
- `POST /api/radio/session/{session_id}/transmissions` - Pilot-Text senden und ATC-Antwort bekommen

## Tests

```bash
poetry run pytest
```