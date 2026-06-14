#!/usr/bin/env python3
"""
rag_index.py — Build Neo's self-knowledge RAG index.

Reads CLAUDE.md (chunked by headings), scenes.json, Python file docstrings,
and .env key names, then inserts them into memory.db as FTS5-searchable chunks.

Run once after setup; re-running clears and rebuilds docs/scene_config/env_keys
chunks. Existing diary entries are never touched.
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

BASE    = Path(__file__).parent
DB_PATH = BASE / "data" / "memory.db"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _ensure_source_type(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "source_type" not in cols:
        conn.execute(
            "ALTER TABLE memories ADD COLUMN source_type TEXT NOT NULL DEFAULT 'legacy'"
        )
        conn.execute(
            "UPDATE memories SET source_type = 'diary' WHERE role = 'diary'"
        )
        conn.commit()
        print("✓ Added source_type column; backfilled diary entries")
    else:
        # Catch any diary rows that slipped through without a type
        conn.execute(
            "UPDATE memories SET source_type = 'diary' "
            "WHERE role = 'diary' AND (source_type IS NULL OR source_type = 'legacy')"
        )
        conn.commit()


def _clear_rag_chunks(conn: sqlite3.Connection) -> None:
    n = conn.execute(
        "DELETE FROM memories WHERE source_type IN ('docs','scene_config','env_keys')"
    ).rowcount
    conn.commit()
    if n:
        print(f"✓ Cleared {n} old RAG chunks")


def _insert(conn: sqlite3.Connection, content: str, source_type: str, source: str) -> None:
    conn.execute(
        "INSERT INTO memories (content, role, source, source_type) "
        "VALUES (?, 'document', ?, ?)",
        (content, source, source_type),
    )


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _chunk_by_headings(text: str, max_words: int = 400) -> list[tuple[str, str]]:
    """Split markdown on ## / ### headings; sub-chunk sections that exceed max_words."""
    sections = re.split(r"\n(?=#{1,3} )", text.strip())
    result = []
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        heading = sec.splitlines()[0].lstrip("#").strip()
        words   = sec.split()
        if len(words) <= max_words:
            result.append((heading, sec))
        else:
            step = max_words - 50  # 50-word overlap between sub-chunks
            for i, start in enumerate(range(0, len(words), step)):
                sub = " ".join(words[start : start + max_words])
                result.append((f"{heading} (part {i + 1})", sub))
    return result


# ---------------------------------------------------------------------------
# Indexers
# ---------------------------------------------------------------------------

def index_claude_md(conn: sqlite3.Connection) -> int:
    path = BASE / "CLAUDE.md"
    if not path.exists():
        print("  CLAUDE.md not found — skipping")
        return 0
    chunks = _chunk_by_headings(path.read_text())
    for heading, body in chunks:
        _insert(conn, body, "docs", f"CLAUDE.md § {heading}")
    conn.commit()
    print(f"✓ CLAUDE.md → {len(chunks)} chunks")
    return len(chunks)


def index_scenes_json(conn: sqlite3.Connection) -> int:
    path = BASE / "scenes.json"
    if not path.exists():
        print("  scenes.json not found — skipping")
        return 0
    scenes = json.loads(path.read_text())

    # One summary chunk listing all scenes
    lines = ["scenes.json — Neo scene definitions:"]
    for name, cfg in scenes.items():
        lamp = cfg.get("lamp", "—")
        tv   = cfg.get("tv", {})
        parts = [f"lamp={lamp}", f"tv={tv.get('action', '—')}"]
        if tv.get("app"):        parts.append(f"app={tv['app']}")
        if tv.get("volume"):     parts.append(f"vol={tv['volume']}")
        if tv.get("volume_abs"): parts.append(f"vol={tv['volume_abs']}")
        if cfg.get("spotify"):   parts.append("spotify=play")
        if cfg.get("presence"):  parts.append(f"presence={cfg['presence']}")
        lines.append(f"  scene '{name}': {', '.join(parts)}")
    _insert(conn, "\n".join(lines), "scene_config", "scenes.json")

    # Per-scene detail chunks for precise retrieval
    for name, cfg in scenes.items():
        detail = f"Scene '{name}': {json.dumps(cfg, indent=2)}"
        _insert(conn, detail, "scene_config", f"scenes.json/{name}")

    conn.commit()
    total = 1 + len(scenes)
    print(f"✓ scenes.json → {total} chunks")
    return total


def index_py_files(conn: sqlite3.Connection) -> int:
    py_files = sorted(
        p for p in BASE.rglob("*.py")
        if "venv" not in p.parts
        and ".git" not in p.parts
        and "neo-labs" not in p.parts
    )
    total = 0
    for path in py_files:
        try:
            text = path.read_text(errors="replace")
            rel  = str(path.relative_to(BASE))

            # Module docstring
            m = re.match(r'\s*"""(.*?)"""', text, re.DOTALL)
            if m:
                doc = m.group(1).strip()
                if len(doc) > 20:
                    _insert(conn, f"Module {rel}:\n{doc}", "docs", rel)
                    total += 1

            # Function and class docstrings
            for m in re.finditer(
                r"(?:^|\n)(?:async\s+)?(?:def|class)\s+(\w+)[^:]*:\s*\n\s+\"\"\"(.*?)\"\"\"",
                text,
                re.DOTALL,
            ):
                doc = m.group(2).strip()
                if len(doc) > 20:
                    _insert(conn, f"{rel} — {m.group(1)}():\n{doc}", "docs", rel)
                    total += 1

        except Exception as exc:
            print(f"  Warning: {path.name}: {exc}")

    conn.commit()
    print(f"✓ Python docstrings → {total} chunks from {len(py_files)} files")
    return total


def index_env_keys(conn: sqlite3.Connection) -> int:
    env_files = sorted(
        p for p in BASE.rglob("*.env")
        if ".git" not in p.parts
        and "venv" not in p.parts
        and not p.name.endswith(".example")
    )
    total = 0
    for path in env_files:
        try:
            keys = [
                line.split("=", 1)[0].strip()
                for line in path.read_text().splitlines()
                if line.strip() and not line.strip().startswith("#") and "=" in line
            ]
            if not keys:
                continue
            rel   = str(path.relative_to(BASE))
            chunk = f"{rel} contains keys: {', '.join(keys)}"
            _insert(conn, chunk, "env_keys", rel)
            total += 1
        except Exception as exc:
            print(f"  Warning: {path.name}: {exc}")

    conn.commit()
    print(f"✓ .env files → {total} chunks")
    return total


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(conn: sqlite3.Connection) -> None:
    print("\nVerification (FTS5 queries across RAG + diary chunks):")
    tests = [
        ("wakeword",  "wakeword"),
        ("voice env", "voice env"),
        ("diary",     "diary"),
    ]
    for label, q in tests:
        try:
            rows = conn.execute(
                """SELECT m.source_type, m.source, m.content
                   FROM memories_fts f JOIN memories m ON m.id = f.rowid
                   WHERE memories_fts MATCH ?
                   AND m.source_type IN ('docs','scene_config','env_keys','diary')
                   ORDER BY rank LIMIT 2""",
                (q,),
            ).fetchall()
            print(f"  '{label}' → {len(rows)} hit(s)")
            for r in rows:
                print(f"    [{r['source_type']}] {r['source']}: {r['content'][:90]}…")
        except Exception as exc:
            print(f"  '{label}' — FTS error: {exc}")

    # Direct diary count (doesn't rely on FTS keyword match)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE role = 'diary'"
        ).fetchone()[0]
        print(f"\n  Diary entries in DB: {n}")
    except Exception as exc:
        print(f"\n  Diary count error: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found.")
        print("Start hub.py once first so memory.init() creates the database.")
        sys.exit(1)

    print(f"RAG indexing → {DB_PATH}\n")
    conn = _conn()
    _ensure_source_type(conn)
    _clear_rag_chunks(conn)

    total  = 0
    total += index_claude_md(conn)
    total += index_scenes_json(conn)
    total += index_py_files(conn)
    total += index_env_keys(conn)

    print(f"\nTotal chunks inserted: {total}")
    verify(conn)
    conn.close()


if __name__ == "__main__":
    main()
