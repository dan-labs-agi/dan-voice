"""Shared episodic memory for Voice Dani.

Stdlib-only SQLite FTS5 store. A TypeScript CLI reads the same DB, so the
schema is fixed. Memory failures must never break a voice turn: writes return
False and reads return [] on any exception, logging a warning instead of raising.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config import config

log = logging.getLogger(__name__)

_DEFAULT_DB = Path.home() / ".dani" / "memory" / "dani.db"

# Chat-template injection tokens to strip before persisting.
_INJECTION_TOKENS = (
    "<|im_start|>",
    "<|im_end|>",
    "<|system|>",
    "</s>",
    "[INST]",
    "[/INST]",
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _db_path() -> Path:
    """Resolve the DB path from config, falling back to the default."""
    return Path(config.memory.db_path) if config.memory.db_path else _DEFAULT_DB


def _sanitize(text: str) -> str:
    """Scrub text before persisting: ANSI escapes, control chars, injection tokens."""
    text = _ANSI_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    for token in _INJECTION_TOKENS:
        text = text.replace(token, "")
    return text


def _connect() -> sqlite3.Connection:
    """Open a WAL-mode connection, creating parent dirs and schema as needed."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS episodic "
        "USING fts5(content, source, ts UNINDEXED)"
    )
    return conn


def retain(content: str, source: str = "voice") -> bool:
    """Persist one episodic memory. Returns False (and warns) on any failure."""
    try:
        clean = _sanitize(content)
        ts = datetime.now(UTC).isoformat()
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO episodic (content, source, ts) VALUES (?, ?, ?)",
                (clean, source, ts),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 - memory must never break a voice turn
        log.warning("memory.retain failed: %s", exc)
        return False


def recall(query: str, limit: int = 5) -> list[tuple[str, str, str]]:
    """Return up to `limit` (ts, source, content) rows matching `query`."""
    if not query or not query.strip():
        return []
    try:
        terms = query.split()
        fts_query = " ".join(f'"{term}"' for term in terms)
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT ts, source, content FROM episodic "
                "WHERE episodic MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, limit),
            )
            return cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - memory must never break a voice turn
        log.warning("memory.recall failed: %s", exc)
        return []
