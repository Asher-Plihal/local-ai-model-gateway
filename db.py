# SQLite persistence for inference request logs and usage analytics queries.
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "usage.db"

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS inference_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    user_id         TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    query_num       INTEGER NOT NULL,
    model           TEXT    NOT NULL,
    prompt_tokens   INTEGER,
    response_tokens INTEGER,
    duration_ms     INTEGER,
    is_new_session  INTEGER NOT NULL DEFAULT 0,
    tokens_per_sec  REAL
)
"""


async def init_db():
    # Create the inference_log table if it doesn't exist; migrate legacy DBs missing tokens_per_sec.
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(CREATE_TABLE)
        # Migrate existing DB if tokens_per_sec column doesn't exist yet
        try:
            await conn.execute("ALTER TABLE inference_log ADD COLUMN tokens_per_sec REAL")
            await conn.commit()
        except Exception:
            pass  # Column already exists


async def get_next_query_num(user_id: str, session_id: str) -> tuple[int, bool]:
    # Return (next_query_number, is_new_session) for the given user+session pair.
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT MAX(query_num) FROM inference_log WHERE user_id=? AND session_id=?",
            (user_id, session_id),
        ) as cursor:
            row = await cursor.fetchone()
            if row[0] is None:
                return 1, True
            return row[0] + 1, False


async def log_inference(
    user_id: str,
    session_id: str,
    query_num: int,
    model: str,
    prompt_tokens: int | None,
    response_tokens: int | None,
    duration_ms: int,
    is_new_session: bool,
    tokens_per_sec: float | None = None,
):
    # Insert one inference_log row with timing and token metrics.
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO inference_log
               (timestamp, user_id, session_id, query_num, model,
                prompt_tokens, response_tokens, duration_ms, is_new_session, tokens_per_sec)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, user_id, session_id, query_num, model,
             prompt_tokens, response_tokens, duration_ms,
             1 if is_new_session else 0, tokens_per_sec),
        )
        await conn.commit()


async def get_summary() -> dict:
    # Return aggregate totals across all inference_log rows.
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("""
            SELECT
                COUNT(*)                              AS total_requests,
                COALESCE(SUM(prompt_tokens), 0)       AS total_prompt_tokens,
                COALESCE(SUM(response_tokens), 0)     AS total_response_tokens,
                COALESCE(SUM(prompt_tokens + response_tokens), 0) AS total_tokens,
                CAST(AVG(duration_ms) AS INTEGER)     AS avg_duration_ms,
                COUNT(DISTINCT user_id)               AS unique_users,
                COUNT(DISTINCT session_id)            AS total_sessions
            FROM inference_log
        """) as cursor:
            row = await cursor.fetchone()
            return dict(row)


async def get_by_user() -> list[dict]:
    # Return per-user token and request counts, ordered by total tokens descending.
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("""
            SELECT
                user_id,
                COUNT(*)                              AS requests,
                COALESCE(SUM(prompt_tokens), 0)       AS prompt_tokens,
                COALESCE(SUM(response_tokens), 0)     AS response_tokens,
                COALESCE(SUM(prompt_tokens + response_tokens), 0) AS total_tokens,
                COUNT(DISTINCT session_id)            AS sessions,
                CAST(AVG(duration_ms) AS INTEGER)     AS avg_duration_ms
            FROM inference_log
            GROUP BY user_id
            ORDER BY total_tokens DESC
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_by_day() -> list[dict]:
    # Return daily token and request counts for the last 30 days, newest first.
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("""
            SELECT
                DATE(timestamp)                       AS date,
                COUNT(*)                              AS requests,
                COALESCE(SUM(prompt_tokens + response_tokens), 0) AS total_tokens,
                COUNT(DISTINCT user_id)               AS unique_users
            FROM inference_log
            GROUP BY DATE(timestamp)
            ORDER BY date DESC
            LIMIT 30
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_sessions(limit: int = 20) -> list[dict]:
    # Return the most recent sessions grouped by session_id, newest last_active first.
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("""
            SELECT
                user_id,
                session_id,
                model,
                MIN(timestamp)                        AS started_at,
                MAX(timestamp)                        AS last_active,
                COUNT(*)                              AS queries,
                COALESCE(SUM(prompt_tokens + response_tokens), 0) AS total_tokens
            FROM inference_log
            GROUP BY session_id
            ORDER BY last_active DESC
            LIMIT ?
        """, (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_speed() -> dict:
    # Return tokens/sec from the most recent timed inference and the last 10.
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        # Last request with real timing data
        async with conn.execute("""
            SELECT tokens_per_sec, response_tokens, timestamp, model
            FROM inference_log
            WHERE tokens_per_sec IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
        """) as cursor:
            last = await cursor.fetchone()

        # Last 10 requests with real timing
        async with conn.execute("""
            SELECT tokens_per_sec, response_tokens, timestamp
            FROM inference_log
            WHERE tokens_per_sec IS NOT NULL
            ORDER BY id DESC
            LIMIT 10
        """) as cursor:
            recent_rows = await cursor.fetchall()

        if not last:
            return {"error": "No speed data yet — make a request first"}

        recent = [dict(r) for r in recent_rows]

        return {
            "last_request": {
                "tokens_per_sec": round(last["tokens_per_sec"], 1),
                "tokens_generated": last["response_tokens"],
                "timestamp": last["timestamp"],
                "model": last["model"],
            },
            "recent_requests": [
                {"tokens_per_sec": round(r["tokens_per_sec"], 1), "tokens_generated": r["response_tokens"]}
                for r in recent
            ],
        }
