# FastAPI proxy that sits in front of Ollama — adds API key auth and per-user usage logging.
import json
import time
import uuid
import httpx
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse

import db

OLLAMA_BASE = "http://localhost:11434"
CONFIG_PATH = Path(__file__).parent / "keys.json"

config: dict = {}


def load_config():
    # Load keys.json into the global config dict; called once at startup.
    global config
    with open(CONFIG_PATH) as f:
        config = json.load(f)


async def require_api_key(request: Request) -> str:
    # Validate API key from header, return the associated user_id. Raises 401 on failure.
    # Accept X-Api-Key header (curl) or Authorization: Bearer <key> (OpenAI client)
    api_key = request.headers.get("X-Api-Key")
    if not api_key:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            api_key = auth[7:]
    if not api_key or api_key not in config.get("keys", {}):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return config["keys"][api_key]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: load config and initialize the SQLite usage database.
    load_config()
    await db.init_db()
    yield


app = FastAPI(title="AI Gateway", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Inference proxy — calls Ollama native /api/chat to capture exact timing
# ---------------------------------------------------------------------------

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    user_id: str = Depends(require_api_key),
):
    # Translate an OpenAI-format chat request to Ollama, log usage, return OpenAI-format response.
    session_id = request.headers.get("X-Session-ID", "default")

    body = await request.json()
    model = body.get("model")

    if not model:
        raise HTTPException(status_code=400, detail="Request must include a 'model' field")

    allowed = config.get("allowed_models", [])
    if model not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model}' is not available. Allowed models: {allowed}",
        )

    # Translate OpenAI-format request → Ollama native format
    ollama_body = {
        "model": model,
        "messages": body.get("messages", []),
        "stream": False,
    }
    if "temperature" in body:
        ollama_body["options"] = {"temperature": body["temperature"]}

    query_num, is_new_session = await db.get_next_query_num(user_id, session_id)

    start = time.monotonic()
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(f"{OLLAMA_BASE}/api/chat", json=ollama_body)
    duration_ms = int((time.monotonic() - start) * 1000)

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    data = resp.json()

    # Extract exact token generation speed from Ollama's timing fields
    eval_count    = data.get("eval_count")     # tokens generated
    eval_duration = data.get("eval_duration")  # nanoseconds spent generating
    prompt_tokens = data.get("prompt_eval_count")
    tokens_per_sec = None
    if eval_count and eval_duration and eval_duration > 0:
        tokens_per_sec = eval_count / (eval_duration / 1e9)

    await db.log_inference(
        user_id=user_id,
        session_id=session_id,
        query_num=query_num,
        model=model,
        prompt_tokens=prompt_tokens,
        response_tokens=eval_count,
        duration_ms=duration_ms,
        is_new_session=is_new_session,
        tokens_per_sec=tokens_per_sec,
    )

    # Translate Ollama response → OpenAI-compatible format
    openai_response = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": data.get("message", {"role": "assistant", "content": ""}),
            "finish_reason": "stop" if data.get("done") else "length",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens or 0,
            "completion_tokens": eval_count or 0,
            "total_tokens": (prompt_tokens or 0) + (eval_count or 0),
        },
    }

    return JSONResponse(content=openai_response)


# ---------------------------------------------------------------------------
# Analytics — no auth, Tailscale-only network is the access control
# ---------------------------------------------------------------------------

@app.get("/usage/summary")
async def usage_summary():
    # Return aggregate totals across all users.
    return await db.get_summary()


@app.get("/usage/by-user")
async def usage_by_user():
    # Return per-user token and request counts.
    return await db.get_by_user()


@app.get("/usage/by-day")
async def usage_by_day():
    # Return daily token and request counts for the last 30 days.
    return await db.get_by_day()


@app.get("/usage/sessions")
async def usage_sessions(limit: int = 20):
    # Return the most recent sessions, grouped by session_id.
    return await db.get_sessions(limit)


@app.get("/usage/speed")
async def usage_speed():
    # Return tokens/sec from the most recent inference and last 10.
    return await db.get_speed()


# ---------------------------------------------------------------------------
# Health — checks Ollama and database
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    # Check Ollama reachability and database connectivity, return combined status.
    ollama_status = "ok"
    database_status = "ok"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_BASE}/api/tags")
            if resp.status_code != 200:
                ollama_status = "unreachable"
    except Exception:
        ollama_status = "unreachable"

    try:
        await db.get_summary()
    except Exception:
        database_status = "error"

    overall = "ok" if ollama_status == "ok" and database_status == "ok" else "degraded"

    return {
        "status": overall,
        "ollama": ollama_status,
        "database": database_status,
    }
