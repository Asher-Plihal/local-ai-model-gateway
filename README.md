# AI Gateway

A lightweight API that sits in front of the local AI models running on this server.
Every inference request is logged — who asked, which model, how many tokens, how long it took.

---

## Access

Any application on either machine can use this — no special setup beyond having the API key.

| Where you're calling from | Base URL |
|---------------------------|----------|
| Anywhere on Tailscale (laptop, phone, etc.) | `http://100.117.45.128:11435` |
| On `goldlobster` itself (local scripts, services) | `http://localhost:11435` |

Tailscale runs as a background service on both machines, so the Tailscale URL works from
any app on your laptop without any extra configuration — Python scripts, VS Code extensions,
cron jobs, other services. If you can run code on the machine, you can call the gateway.

---

## Authentication

Only the inference endpoint requires an API key. Analytics and health endpoints are open
to anyone on the Tailscale network — the network itself is the access control.

| User  | API Key                                  |
|-------|------------------------------------------|
| asher | `ak_asher_5aecc347cf222c56b00fd697`      |
| avi   | `ak_avi_1e43962517564c01bbac9941`        |

The API key can be passed two ways — both work:
- `X-Api-Key: <key>` header (curl, direct HTTP calls)
- `api_key` argument in the OpenAI Python client (sent automatically as `Authorization: Bearer <key>`)

---

## Available models

| Model              | ID to use in requests  |
|--------------------|------------------------|
| Qwen 2.5 Coder 32B | `qwen2.5-coder:32b`    |

Requesting any other model returns a `400` with a clear error.
To add a model: `ollama pull <model>`, add it to `keys.json`, restart the service.

---

## Endpoints

### `POST /v1/chat/completions` — Send a message *(requires API key)*

Standard OpenAI-format chat request. Works with any OpenAI-compatible client or library.
**Dependency:** `pip install openai`

**Headers:**
- `X-Api-Key` — your API key *(required)*
- `X-Session-ID` — groups messages into a conversation for tracking purposes *(optional)*

**Sessions explained:**
The model has no memory between requests — you send the full conversation history in the
`messages` array every time. A session ID is just a label that groups your requests together
in the usage logs. Use any string. Generate a new one when starting a fresh conversation,
reuse it for follow-up messages.

**curl example:**
```bash
curl -X POST http://100.117.45.128:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ak_asher_5aecc347cf222c56b00fd697" \
  -H "X-Session-ID: my-session-001" \
  -d '{
    "model": "qwen2.5-coder:32b",
    "messages": [
      {"role": "user", "content": "Explain what a REST API is in one sentence."}
    ]
  }'
```

**Python example (multi-turn conversation):**
```python
import uuid
from openai import OpenAI

client = OpenAI(
    base_url="http://100.117.45.128:11435/v1",
    api_key="ak_asher_5aecc347cf222c56b00fd697",
)

session_id = str(uuid.uuid4())  # new ID = new session in the logs

# First message
r1 = client.chat.completions.create(
    model="qwen2.5-coder:32b",
    messages=[
        {"role": "user", "content": "My name is Asher."}
    ],
    extra_headers={"X-Session-ID": session_id},
)

# Follow-up — same session_id, full history in messages
r2 = client.chat.completions.create(
    model="qwen2.5-coder:32b",
    messages=[
        {"role": "user",      "content": "My name is Asher."},
        {"role": "assistant", "content": r1.choices[0].message.content},
        {"role": "user",      "content": "What's my name?"},
    ],
    extra_headers={"X-Session-ID": session_id},
)

print(r2.choices[0].message.content)
```

**Calling from the server itself** — replace the Tailscale IP with localhost:
```python
client = OpenAI(
    base_url="http://localhost:11435/v1",
    api_key="ak_asher_5aecc347cf222c56b00fd697",
)
```

---

### `GET /usage/summary` — Overall usage

Total requests, tokens, and users across all time. No API key needed.

```bash
curl http://100.117.45.128:11435/usage/summary
```

```json
{
  "total_requests": 10,
  "total_prompt_tokens": 820,
  "total_response_tokens": 340,
  "total_tokens": 1160,
  "avg_duration_ms": 4200,
  "unique_users": 2,
  "total_sessions": 5
}
```

---

### `GET /usage/by-user` — Per-user breakdown

Token and request counts per user, sorted by total tokens descending. No API key needed.

```bash
curl http://100.117.45.128:11435/usage/by-user
```

```json
[
  {
    "user_id": "asher",
    "requests": 6,
    "prompt_tokens": 497,
    "response_tokens": 541,
    "total_tokens": 1038,
    "sessions": 4,
    "avg_duration_ms": 20994
  }
]
```

---

### `GET /usage/by-day` — Daily totals

Last 30 days of activity, newest first. No API key needed.

```bash
curl http://100.117.45.128:11435/usage/by-day
```

```json
[
  {
    "date": "2026-04-15",
    "requests": 4,
    "total_tokens": 958,
    "unique_users": 1
  },
  {
    "date": "2026-04-14",
    "requests": 2,
    "total_tokens": 80,
    "unique_users": 1
  }
]
```

---

### `GET /usage/sessions?limit=20` — Recent sessions

Most recent conversations with per-session token totals, newest first. No API key needed.
Default limit is 20, pass `?limit=N` for more.

```bash
curl http://100.117.45.128:11435/usage/sessions
curl http://100.117.45.128:11435/usage/sessions?limit=50
```

```json
[
  {
    "user_id": "asher",
    "session_id": "ae5bfdb9-d438-4cee-beff-eb20860d5aea",
    "model": "qwen2.5-coder:32b",
    "started_at": "2026-04-15T01:37:07.084134+00:00",
    "last_active": "2026-04-15T01:38:23.221984+00:00",
    "queries": 2,
    "total_tokens": 878
  }
]
```

---

### `GET /usage/speed` — Token generation speed

Exact tokens per second from the last request, measured using Ollama's internal
`eval_duration` (pure generation time, not wall-clock). No API key needed.

```bash
curl http://100.117.45.128:11435/usage/speed
```

```json
{
  "last_request": {
    "tokens_per_sec": 4.9,
    "tokens_generated": 60,
    "timestamp": "2026-04-15T01:56:41.991822+00:00",
    "model": "qwen2.5-coder:32b"
  },
  "recent_requests": [
    {"tokens_per_sec": 4.9, "tokens_generated": 60},
    {"tokens_per_sec": 5.1, "tokens_generated": 24}
  ]
}
```

Returns `{"error": "No speed data yet — make a request first"}` if no requests have been made.

---

### `GET /health` — Service health

Checks that both Ollama and the database are reachable. No API key needed.

```bash
curl http://100.117.45.128:11435/health
```

```json
{"status": "ok", "ollama": "ok", "database": "ok"}
```

If something is down, `status` becomes `"degraded"` and the affected component shows its error.

---

## Notes

- **First request after idle:** The model unloads from GPU memory after ~5 minutes of inactivity.
  The first request after that takes ~10 seconds to respond while it reloads. Subsequent requests are fast.
- **No streaming:** Responses are returned all at once. This lets the gateway capture accurate
  token counts before logging.
- **Adding/revoking users:** Edit `keys.json` and restart the service (`sudo systemctl restart ai-gateway`).
- **Usage database:** `/home/goldlobster/ai-gateway/usage.db`
- **Logs:** `journalctl -u ai-gateway -f`