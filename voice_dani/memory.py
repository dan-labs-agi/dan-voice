"""Shared episodic memory for Voice Dani.

Stdlib-only SQLite FTS5 store. A TypeScript CLI reads the same DB, so the
schema is fixed. Memory failures must never break a voice turn: writes return
False and reads return [] on any exception, logging a warning instead of raising.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config import config

log = logging.getLogger(__name__)

_DEFAULT_DB = Path.home() / ".dani" / "memory" / "dani.db"

# Canonical FTS5 schema (CONTRACT C1). The TypeScript CLI reads this exact
# shape; porter stemming lets 'decision' match 'deciding'.
_EPISODIC_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS episodic "
    "USING fts5(content, source, ts UNINDEXED, tokenize='porter unicode61')"
)

# Core memory (CONTRACT C2): a human-editable markdown file next to the DB.
_CORE_HEADER = "# Dani Core Memory"

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


def _migrate_episodic(conn: sqlite3.Connection) -> None:
    """Migrate a pre-porter `episodic` table to the porter-tokenized schema.

    Idempotent: only acts when the table exists and its DDL lacks 'porter'.
    Never raises — a failed migration logs a warning and leaves data intact.
    """
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='episodic'"
        ).fetchone()
        if row is None or not row[0]:
            return
        if "porter" in row[0].lower():
            return
        conn.execute("BEGIN")
        conn.execute(
            "CREATE VIRTUAL TABLE episodic_new "
            "USING fts5(content, source, ts UNINDEXED, tokenize='porter unicode61')"
        )
        conn.execute(
            "INSERT INTO episodic_new(content, source, ts) "
            "SELECT content, source, ts FROM episodic"
        )
        conn.execute("DROP TABLE episodic")
        conn.execute("ALTER TABLE episodic_new RENAME TO episodic")
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - migration must never break a voice turn
        log.warning("memory migration failed: %s", exc)
        with contextlib.suppress(Exception):
            conn.rollback()


def _connect() -> sqlite3.Connection:
    """Open a WAL-mode connection, creating parent dirs and schema as needed."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate_episodic(conn)
    conn.execute(_EPISODIC_SCHEMA)
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


# ---------------------------------------------------------------------------
# Core memory blocks (CONTRACT C2) — persona/preferences in core.md
# ---------------------------------------------------------------------------

def _core_path() -> Path:
    """Resolve core.md, which lives next to the DB file."""
    return _db_path().parent / "core.md"


def load_core() -> str:
    """Return core.md content, or "" if missing/unreadable. Never raises."""
    try:
        return _core_path().read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - core memory is best-effort
        log.debug("memory.load_core: %s", exc)
        return ""


def set_core_block(block: str, text: str) -> bool:
    """Replace the `## <block>` section body in core.md, preserving others.

    Creates the file/section if missing. Text is sanitized. Never raises.
    """
    try:
        clean = _sanitize(text).strip()
        path = _core_path()
        try:
            existing = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""

        # Parse existing "## <name>" sections into an ordered list.
        sections: list[tuple[str, str]] = []
        current_name: str | None = None
        current_lines: list[str] = []
        for line in existing.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                if current_name is not None:
                    sections.append(
                        (current_name, "\n".join(current_lines).strip())
                    )
                current_name = stripped[3:].strip()
                current_lines = []
            elif current_name is not None:
                current_lines.append(line)
        if current_name is not None:
            sections.append((current_name, "\n".join(current_lines).strip()))

        # Replace the target block in place, or append it if absent.
        for i, (name, _body) in enumerate(sections):
            if name == block:
                sections[i] = (block, clean)
                break
        else:
            sections.append((block, clean))

        # Render canonical form: fixed header, then each "## <name>" + body.
        parts = [_CORE_HEADER, ""]
        for name, body in sections:
            parts.append(f"## {name}")
            if body:
                parts.append(body)
            parts.append("")
        rendered = "\n".join(parts).rstrip() + "\n"

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001 - core memory is best-effort
        log.warning("memory.set_core_block failed: %s", exc)
        return False
