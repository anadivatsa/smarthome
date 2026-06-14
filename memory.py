"""
memory.py — Persistent vector memory for Neo smart home hub.

Embedding fallback chain (auto-detected at startup):
  1. fastembed BAAI/bge-small-en-v1.5 + sqlite-vec  → KNN vector search
  2. fastembed + numpy cosine similarity             → in-process vector search
  3. SQLite FTS5                                     → full-text fallback

Never crashes regardless of which optional deps are installed.
Model loads in a background thread; FTS5 is used until it's ready.
"""

import html as _html_mod
import html.parser
import json
import logging
import os
import re
import shutil
import sqlite3
import struct
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("memory")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_BASE      = Path(__file__).parent
_DATA      = _BASE / "data"
_DB_PATH   = _DATA / "memory.db"
_CONV_MAX  = 1000   # max rows in conversation table before trimming

# ---------------------------------------------------------------------------
# Embedding backend (lazy, background-loaded)
# ---------------------------------------------------------------------------

_BACKEND     = "fts"          # "vec" | "numpy" | "fts"
_embed_model = None
_embed_lock  = threading.Lock()
_embed_ready = threading.Event()


def _load_embedding_model() -> None:
    """Background thread: try to load fastembed + detect vector backend."""
    global _BACKEND, _embed_model
    try:
        log.info("memory: loading BAAI/bge-small-en-v1.5 (first run downloads ~45 MB)…")
        from fastembed import TextEmbedding  # noqa: PLC0415
        model = TextEmbedding("BAAI/bge-small-en-v1.5")
        list(model.embed(["warmup"]))           # force model download + warm up

        # Probe sqlite-vec by loading it in a throw-away connection
        vec_ok = False
        try:
            import sqlite_vec                   # noqa: PLC0415
            tc = sqlite3.connect(":memory:")
            tc.enable_load_extension(True)
            sqlite_vec.load(tc)
            tc.enable_load_extension(False)
            tc.close()
            vec_ok = True
        except Exception:
            pass

        with _embed_lock:
            _embed_model = model
            _BACKEND = "vec" if vec_ok else "numpy"
        log.info("memory: embedding backend = %s", _BACKEND)

    except Exception as exc:
        log.warning("memory: fastembed unavailable (%s) — using FTS5 fallback", exc)
    finally:
        _embed_ready.set()


def _embed(texts: list[str]) -> list[Optional[bytes]]:
    """Return serialised float32 bytes per text, or None if embedder not ready."""
    with _embed_lock:
        model = _embed_model
    if model is None:
        return [None] * len(texts)
    try:
        import numpy as np                      # noqa: PLC0415
        vecs = list(model.embed(texts))
        return [np.array(v, dtype=np.float32).tobytes() for v in vecs]
    except Exception:
        return [None] * len(texts)


# ---------------------------------------------------------------------------
# SQLite connection (thread-local, WAL mode)
# ---------------------------------------------------------------------------

_local = threading.local()


def _conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        _DATA.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        # Try loading sqlite-vec extension into this connection
        try:
            import sqlite_vec                   # noqa: PLC0415
            c.enable_load_extension(True)
            sqlite_vec.load(c)
            c.enable_load_extension(False)
        except Exception:
            pass
        _local.conn = c
    return _local.conn


# ---------------------------------------------------------------------------
# Public: init
# ---------------------------------------------------------------------------

def init() -> None:
    """Create tables and start background embedding loader. Call once at startup."""
    _DATA.mkdir(parents=True, exist_ok=True)
    c = _conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            content     TEXT    NOT NULL,
            role        TEXT    NOT NULL DEFAULT 'user',
            source      TEXT    NOT NULL DEFAULT 'manual',
            source_type TEXT    NOT NULL DEFAULT 'legacy',
            embedding   BLOB,
            timestamp   TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
            USING fts5(content, content='memories', content_rowid='id');

        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
        END;

        CREATE TABLE IF NOT EXISTS conversation (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            role      TEXT    NOT NULL,
            content   TEXT    NOT NULL,
            timestamp TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS scene_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            scene        TEXT    NOT NULL,
            triggered_by TEXT    NOT NULL DEFAULT 'api',
            timestamp    TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sensor_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_type TEXT    NOT NULL,
            value       REAL    NOT NULL,
            unit        TEXT    NOT NULL DEFAULT '',
            timestamp   TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """)
    c.commit()

    # Migrate existing installs: add source_type column if it was created before this upgrade
    try:
        c.execute("ALTER TABLE memories ADD COLUMN source_type TEXT NOT NULL DEFAULT 'legacy'")
        c.execute("UPDATE memories SET source_type = 'diary' WHERE role = 'diary'")
        c.commit()
        log.info("memory: added source_type column")
    except Exception:
        pass  # Column already exists — normal on subsequent startups

    # Start background model loader (daemon — hub continues if it fails)
    if not _embed_ready.is_set():
        t = threading.Thread(target=_load_embedding_model, daemon=True, name="memory-embed-loader")
        t.start()


# ---------------------------------------------------------------------------
# Public: conversation
# ---------------------------------------------------------------------------

def store_conversation(role: str, content: str) -> None:
    """Append one turn to the rolling conversation log."""
    c = _conn()
    c.execute("INSERT INTO conversation (role, content) VALUES (?, ?)", (role, content))
    # Trim to CONV_MAX rows
    c.execute("""
        DELETE FROM conversation WHERE id IN (
            SELECT id FROM conversation ORDER BY id ASC
            LIMIT MAX(0, (SELECT COUNT(*) FROM conversation) - ?)
        )
    """, (_CONV_MAX,))
    c.commit()


def get_recent(n: int = 15) -> list[dict]:
    """Return last n conversation turns as list of {role, content} dicts for Claude API."""
    rows = _conn().execute(
        "SELECT role, content FROM conversation ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ---------------------------------------------------------------------------
# Public: long-term memory
# ---------------------------------------------------------------------------

def store_memory(content: str, role: str = "user", source: str = "manual") -> int:
    """Embed and persist a memory. Returns the new row id."""
    c = _conn()
    (emb,) = _embed([content])
    cur = c.execute(
        "INSERT INTO memories (content, role, source, embedding) VALUES (?, ?, ?, ?)",
        (content, role, source, emb),
    )
    c.commit()
    return cur.lastrowid


def search(query: str, n: int = 5) -> list[dict]:
    """Semantic (or FTS) search over long-term memories. Returns list of dicts."""
    with _embed_lock:
        backend = _BACKEND
        model   = _embed_model

    # --- vector backends ---------------------------------------------------
    if backend in ("vec", "numpy") and model is not None:
        try:
            import numpy as np                  # noqa: PLC0415
            (q_bytes,) = _embed([query])
            if q_bytes is None:
                raise ValueError("embed returned None")
            q_vec = np.frombuffer(q_bytes, dtype=np.float32)

            rows = _conn().execute(
                "SELECT id, content, role, source, timestamp, embedding FROM memories "
                "WHERE embedding IS NOT NULL"
            ).fetchall()

            if not rows:
                return _fts_search(query, n)

            scored = []
            for row in rows:
                v = np.frombuffer(row["embedding"], dtype=np.float32)
                norm = np.linalg.norm(q_vec) * np.linalg.norm(v)
                sim = float(np.dot(q_vec, v) / norm) if norm > 1e-8 else 0.0
                scored.append((sim, row))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [_row_to_dict(r) for _, r in scored[:n]]

        except Exception as exc:
            log.debug("memory: vector search failed (%s), falling back to FTS", exc)

    # --- FTS5 fallback -----------------------------------------------------
    return _fts_search(query, n)


def _fts_search(query: str, n: int) -> list[dict]:
    try:
        rows = _conn().execute(
            """SELECT m.id, m.content, m.role, m.source, m.timestamp
               FROM memories_fts f JOIN memories m ON m.id = f.rowid
               WHERE memories_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (query, n),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        # Plain LIKE fallback if FTS match syntax fails
        rows = _conn().execute(
            "SELECT id, content, role, source, timestamp FROM memories "
            "WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{query}%", n),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def _row_to_dict(row) -> dict:
    return {
        "id":        row["id"],
        "content":   row["content"],
        "role":      row["role"],
        "source":    row["source"],
        "timestamp": row["timestamp"],
    }


# ---------------------------------------------------------------------------
# Public: scene + sensor logging
# ---------------------------------------------------------------------------

def store_scene_event(scene: str, triggered_by: str = "api") -> None:
    """Log a scene activation with source (nfc / siri / telegram / hub_dashboard / api)."""
    try:
        c = _conn()
        c.execute(
            "INSERT INTO scene_log (scene, triggered_by) VALUES (?, ?)",
            (scene, triggered_by),
        )
        c.commit()
    except Exception as exc:
        log.debug("memory.store_scene_event failed: %s", exc)


def get_scene_history(scene: Optional[str] = None, n: int = 20) -> list[dict]:
    """Return last n scene activations, optionally filtered by scene name."""
    if scene:
        rows = _conn().execute(
            "SELECT scene, triggered_by, timestamp FROM scene_log "
            "WHERE scene = ? ORDER BY id DESC LIMIT ?", (scene, n)
        ).fetchall()
    else:
        rows = _conn().execute(
            "SELECT scene, triggered_by, timestamp FROM scene_log "
            "ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    return [dict(r) for r in rows]


def store_sensor_reading(sensor_type: str, value: float, unit: str = "") -> None:
    """Persist a sensor reading (future GPIO / light sensor use)."""
    try:
        c = _conn()
        c.execute(
            "INSERT INTO sensor_log (sensor_type, value, unit) VALUES (?, ?, ?)",
            (sensor_type, float(value), unit),
        )
        c.commit()
    except Exception as exc:
        log.debug("memory.store_sensor_reading failed: %s", exc)


# ---------------------------------------------------------------------------
# Public: ingestion helpers
# ---------------------------------------------------------------------------

def _chunk_text(text: str, size: int = 300, overlap: int = 50) -> list[str]:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunk = " ".join(words[i : i + size])
        if chunk.strip():
            chunks.append(chunk)
        i += size - overlap
    return chunks or [text[:2000]]


def ingest_url(url: str) -> int:
    """Fetch a webpage, chunk it, embed and store. Returns number of chunks stored."""
    import urllib.request                       # noqa: PLC0415
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise RuntimeError(f"ingest_url: could not fetch {url}: {exc}") from exc

    # Strip HTML tags with stdlib
    class _Stripper(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self._parts: list[str] = []
            self._skip = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "nav", "footer", "header"):
                self._skip = True

        def handle_endtag(self, tag):
            if tag in ("script", "style", "nav", "footer", "header"):
                self._skip = False

        def handle_data(self, data):
            if not self._skip:
                self._parts.append(data)

        def get_text(self):
            return " ".join(self._parts)

    stripper = _Stripper()
    stripper.feed(raw)
    text = re.sub(r"\s+", " ", stripper.get_text()).strip()

    chunks = _chunk_text(text)
    embeddings = _embed(chunks)
    c = _conn()
    for chunk, emb in zip(chunks, embeddings):
        c.execute(
            "INSERT INTO memories (content, role, source, embedding) VALUES (?, ?, ?, ?)",
            (chunk, "document", url, emb),
        )
    c.commit()
    return len(chunks)


def ingest_pdf(path: str) -> int:
    """Extract text from a PDF, chunk, embed and store. Returns chunks stored."""
    text = ""
    try:
        import pypdf                            # noqa: PLC0415
        reader = pypdf.PdfReader(path)
        text = " ".join(p.extract_text() or "" for p in reader.pages)
    except ImportError:
        pass

    if not text:
        try:
            from pdfminer.high_level import extract_text as pdfminer_extract  # noqa: PLC0415
            text = pdfminer_extract(path)
        except ImportError:
            raise RuntimeError(
                "ingest_pdf requires pypdf or pdfminer.six: "
                "pip install pypdf  OR  pip install pdfminer.six"
            )

    text = re.sub(r"\s+", " ", text).strip()
    chunks = _chunk_text(text)
    embeddings = _embed(chunks)
    c = _conn()
    for chunk, emb in zip(chunks, embeddings):
        c.execute(
            "INSERT INTO memories (content, role, source, embedding) VALUES (?, ?, ?, ?)",
            (chunk, "document", str(path), emb),
        )
    c.commit()
    return len(chunks)


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def prune_conversation(days: int = 30) -> int:
    """Delete conversation rows older than `days` days. Returns deleted count."""
    c = _conn()
    cur = c.execute(
        "DELETE FROM conversation WHERE timestamp < datetime('now', ?)",
        (f"-{days} days",),
    )
    c.commit()
    return cur.rowcount
