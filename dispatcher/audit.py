"""Журнал событий с очисткой секретов."""

import json
import re
import sqlite3
from datetime import datetime, timezone

from dispatcher import config

KEY_PATTERN = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,})")
MASK = "***"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact(text: str) -> str:
    """Убирает из текста значение токена и строки, похожие на ключи."""
    if config.AI_PROXY_TOKEN:
        text = text.replace(config.AI_PROXY_TOKEN, MASK)
    return KEY_PATTERN.sub(MASK, text)


def write(conn: sqlite3.Connection, case_id: str | None, actor: str, event: str, details: dict) -> None:
    payload = redact(json.dumps(details, ensure_ascii=False))
    conn.execute(
        "INSERT INTO audit_log (ts, case_id, actor, event, details) VALUES (?, ?, ?, ?, ?)",
        (now(), case_id, actor, event, payload),
    )
