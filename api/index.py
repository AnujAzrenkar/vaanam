"""
Vaanam enquiry backend — a single Vercel serverless function.

Routes (Vercel serves this whole app under /api/* automatically):
  GET  /api/health   -> quick liveness check
  POST /api/enquiry  -> validate + store an enquiry in Postgres

Why this shape:
  - Vercel functions are stateless/ephemeral, so we persist to an EXTERNAL
    Postgres (Vercel Postgres / Neon / Supabase) reached via the POSTGRES_URL
    environment variable. Never use SQLite-on-disk here — it disappears.
  - psycopg (v3) opens a fresh short-lived connection per request. That's the
    correct pattern for serverless: no long-lived pool to leak across freezes.
"""

import os
from datetime import datetime, timezone

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, field_validator

app = FastAPI(title="Vaanam Enquiry API")

# ---- Request schema -------------------------------------------------------
# Pydantic validates the incoming JSON before any DB work happens.
class Enquiry(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr
    dates: str | None = Field(default=None, max_length=120)
    cottage: str | None = Field(default=None, max_length=80)
    message: str | None = Field(default=None, max_length=2000)

    @field_validator("name", "dates", "cottage", "message")
    @classmethod
    def strip_blanks(cls, v):
        if v is None:
            return v
        v = v.strip()
        return v or None


# ---- DB helpers -----------------------------------------------------------
def _conn():
    """Open a new connection. POSTGRES_URL is set in Vercel env vars."""
    url = os.environ.get("POSTGRES_URL")
    if not url:
        raise RuntimeError("POSTGRES_URL is not set")
    return psycopg.connect(url, connect_timeout=10)


def _ensure_table():
    """Create the enquiries table if it doesn't exist (idempotent)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS enquiries (
                id          BIGSERIAL PRIMARY KEY,
                name        TEXT NOT NULL,
                email       TEXT NOT NULL,
                dates       TEXT,
                cottage     TEXT,
                message     TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.commit()


# ---- Routes ---------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}


@app.post("/api/enquiry")
def create_enquiry(enquiry: Enquiry):
    try:
        _ensure_table()  # cheap; safe to call every request
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO enquiries (name, email, dates, cottage, message)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, created_at;
                """,
                (
                    enquiry.name,
                    str(enquiry.email),
                    enquiry.dates,
                    enquiry.cottage,
                    enquiry.message,
                ),
            )
            new_id, created_at = cur.fetchone()
            conn.commit()
        return {"ok": True, "id": new_id, "created_at": created_at.isoformat()}
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the client
        # Log full detail server-side (shows in Vercel function logs),
        # return a generic message to the browser.
        print(f"[enquiry] error: {exc!r}")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "Could not save enquiry. Please try again."},
        )
