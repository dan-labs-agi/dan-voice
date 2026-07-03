"""Neural semantic memory layer for Voice Dani (Phase 3).

Optional, graceful-degradation companion to the FTS5 episodic store in
`memory.py`. Content is embedded with fastembed (BAAI/bge-small-en-v1.5) and
searched by KNN via a sqlite-vec `vec0` virtual table (`vec_episodic`) that
lives in the SAME db file as `episodic`, linked back by rowid.

The whole module is best-effort and never breaks a voice turn: if the
`semantic` extra is not installed, or any operation fails, functions log a
warning and return False / [] / 0. The ONE exception is the __main__ CLI —
when the extras are missing it prints an install hint and exits 1.

Model files are cached under ~/.dani/models (override via FASTEMBED_CACHE_PATH)
so they are discoverable and cleanable.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sqlite3
import sys
from functools import lru_cache
from pathlib import Path

from .config import config

log = logging.getLogger(__name__)

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
# Embedding dimensionality of EMBED_MODEL; hardcoded in the DDL literal below
# (kept out of an f-string so ruff's SQL rules stay happy).
EMBED_DIM = 384

_DEFAULT_DB = Path.home() / ".dani" / "memory" / "dani.db"
_DEFAULT_MODEL_CACHE = Path.home() / ".dani" / "models"

# sqlite-vec vec0 table sharing episodic's db file. The implicit rowid is set
# to the matching `episodic` rowid on insert, giving a stable join key.
_VEC_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS vec_episodic "
    "USING vec0(embedding float[384])"
)

_INSTALL_HINT = "semantic extras not installed - run: pip/uv install 'dan-voice[semantic]'"


@lru_cache(maxsize=1)
def available() -> bool:
    """Return True iff both fastembed and sqlite_vec are importable. Cached."""
    return (
        importlib.util.find_spec("fastembed") is not None
        and importlib.util.find_spec("sqlite_vec") is not None
    )


def _db_path() -> Path:
    """Resolve the shared episodic DB path (same logic as memory._db_path)."""
    return Path(config.memory.db_path) if config.memory.db_path else _DEFAULT_DB


@lru_cache(maxsize=1)
def _model():  # noqa: ANN202 - fastembed.TextEmbedding, imported lazily
    """Load and cache the embedding model, honouring the model cache dir."""
    from fastembed import TextEmbedding

    cache_dir = os.environ.get("FASTEMBED_CACHE_PATH") or str(_DEFAULT_MODEL_CACHE)
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    return TextEmbedding(model_name=EMBED_MODEL, cache_dir=cache_dir)


def _embed(text: str) -> bytes:
    """Embed one string and serialize it to sqlite-vec float32 blob form."""
    from sqlite_vec import serialize_float32

    vector = next(iter(_model().embed([text])))
    return serialize_float32(vector.tolist())


def _connect() -> sqlite3.Connection:
    """Open the shared DB with the sqlite-vec extension loaded and vec table ready."""
    import sqlite_vec

    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(_VEC_SCHEMA)
    return conn


def index(content: str, rowid: int) -> bool:
    """Embed `content` and store it against `rowid` in vec_episodic.

    Idempotent: re-indexing a rowid replaces its vector. Returns False (and
    warns) if extras are missing or anything fails.
    """
    if not available():
        return False
    try:
        blob = _embed(content)
        conn = _connect()
        try:
            conn.execute("DELETE FROM vec_episodic WHERE rowid = ?", (rowid,))
            conn.execute(
                "INSERT INTO vec_episodic(rowid, embedding) VALUES (?, ?)",
                (rowid, blob),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 - semantic must never break a voice turn
        log.warning("semantic.index failed: %s", exc)
        return False


def search(query: str, limit: int = 5) -> list[tuple[str, str, str]]:
    """Return up to `limit` (ts, source, content) episodic rows nearest to `query`."""
    if not available():
        return []
    if not query or not query.strip():
        return []
    try:
        blob = _embed(query)
        conn = _connect()
        try:
            cur = conn.execute(
                "WITH knn AS ("
                "  SELECT rowid, distance FROM vec_episodic "
                "  WHERE embedding MATCH ? ORDER BY distance LIMIT ?"
                ") "
                "SELECT e.ts, e.source, e.content "
                "FROM knn JOIN episodic e ON e.rowid = knn.rowid "
                "ORDER BY knn.distance",
                (blob, limit),
            )
            return cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - semantic must never break a voice turn
        log.warning("semantic.search failed: %s", exc)
        return []


def reindex() -> int:
    """Backfill: embed every episodic row not yet in vec_episodic. Returns count."""
    if not available():
        return 0
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT rowid, content FROM episodic "
                "WHERE rowid NOT IN (SELECT rowid FROM vec_episodic)"
            ).fetchall()
            count = 0
            for rowid, content in rows:
                conn.execute(
                    "INSERT INTO vec_episodic(rowid, embedding) VALUES (?, ?)",
                    (rowid, _embed(content)),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - semantic must never break a voice turn
        log.warning("semantic.reindex failed: %s", exc)
        return 0


def _main(argv: list[str]) -> int:
    """CLI entry: `reindex` or `search <query>`. Exits 1 if extras missing."""
    if not available():
        print(_INSTALL_HINT, file=sys.stderr)
        return 1
    if not argv:
        print("usage: python -m voice_dani.semantic {reindex|search <query>}", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "reindex":
        print(reindex())
        return 0
    if cmd == "search":
        query = " ".join(rest).strip()
        if not query:
            print("usage: python -m voice_dani.semantic search <query>", file=sys.stderr)
            return 2
        for ts, source, content in search(query):
            snippet = content[:120].replace("\n", " ")
            print(f"[{ts}] ({source}) {snippet}")
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
