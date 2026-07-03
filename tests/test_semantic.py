"""Tests for voice_dani.semantic — neural semantic memory layer (Phase 3).

Only one test runs unconditionally (graceful degradation). The rest are
skipped when the `semantic` extra is not installed.
"""

import sqlite3

import pytest

from voice_dani import memory, semantic


@pytest.fixture
def sem_db(tmp_path, monkeypatch):
    """Point both memory and semantic at one throwaway DB (shared config singleton)."""
    db = tmp_path / "dani.db"
    monkeypatch.setattr(memory.config.memory, "db_path", str(db))
    return db


def test_available_bool_and_graceful_search():
    """Always runs: available() is a bool and search() degrades to [] when unavailable."""
    assert isinstance(semantic.available(), bool)
    if not semantic.available():
        assert semantic.search("authentication method") == []
        assert semantic.index("anything", 1) is False
        assert semantic.reindex() == 0


@pytest.mark.skipif(not semantic.available(), reason="semantic extras not installed")
def test_index_and_search_ranks_pin_first(sem_db):
    memory.retain("the tunnel keeps dying on wake", source="voice")
    memory.retain("we chose PIN pairing for auth", source="voice")

    con = sqlite3.connect(sem_db)
    try:
        rows = con.execute("SELECT rowid, content FROM episodic ORDER BY rowid").fetchall()
    finally:
        con.close()
    assert len(rows) == 2
    for rowid, content in rows:
        assert semantic.index(content, rowid) is True

    results = semantic.search("authentication method", limit=5)
    assert results, "expected at least one semantic hit"
    _ts, _source, content = results[0]
    assert "PIN" in content


@pytest.mark.skipif(not semantic.available(), reason="semantic extras not installed")
def test_reindex_backfills_and_is_idempotent(sem_db):
    memory.retain("alpha content about tunnels", source="voice")
    memory.retain("beta content about pairing", source="voice")

    assert semantic.reindex() == 2
    # Nothing left to backfill on a second pass.
    assert semantic.reindex() == 0


@pytest.mark.skipif(not semantic.available(), reason="semantic extras not installed")
def test_search_empty_query_returns_empty(sem_db):
    memory.retain("some content", source="voice")
    semantic.reindex()
    assert semantic.search("") == []
    assert semantic.search("   ") == []
