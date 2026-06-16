# Architecture Philosophy

Standing decisions for this project. Don't re-litigate these in future sessions — they were reached after real failures, not theory.

---

## Telegram voice bot over always-on local mic

**Decision:** The primary voice input is Telegram voice messages (`tgvoice.py`), not a continuously-listening local mic pipeline.

**Why:** The always-on mic approach was tried and failed repeatedly: AudioRelay was abandoned (unstable ARM alpha build), Termux mic access required a multi-step Android permission dance that broke after updates, and PyAudio device indices shifted across reboots causing crash loops. Telegram is reliable, works over any network, requires no persistent connection, and the latency is acceptable for home automation. Novelty is not worth the fragility.

**Implication:** Don't propose mic-first features. The `voice.service` exists as a secondary path but is not the primary interface.

---

## FTS5 keyword search over vector embeddings for RAG/memory

**Decision:** The self-knowledge RAG system (`rag_index.py`, `memory.py`) uses SQLite FTS5 full-text search, not a vector embedding store.

**Why:** The corpus is small (~116 chunks: CLAUDE.md sections, scenes.json, docstrings, env key names). At this scale, FTS5 AND→OR fallback gives fast, accurate, zero-dependency retrieval. Vector embeddings would require a model (more venv weight), an embedding API call or local inference (latency + cost), and a vector DB or numpy similarity search — all complexity with no proportional benefit when the entire knowledge base fits in one SQLite file.

**Implication:** Don't suggest adding embeddings, semantic similarity, or vector stores to the memory/RAG layer unless the corpus grows by 100×.

---

## systemd services over cron jobs or manual scripts

**Decision:** Every persistent process runs as a systemd service with `Restart=on-failure`.

**Why:** Cron is fire-and-forget and doesn't restart on crash. Manual scripts require SSH and attention. systemd gives automatic restart, `journalctl` logging, `systemctl status` in one command, and proper dependency ordering (e.g. `After=network.target`). The `voice.service` crash-loop incident (762 restarts) was caught via journalctl — without systemd it would have been invisible.

**Implication:** New persistent processes get a `.service` file. One-shot scheduled tasks go in `scheduler.py` via APScheduler (already running inside `tgvoice.service`), not a new cron entry.

---

## General principle: simplest proven pattern first

> When in doubt, default to the simplest pattern already proven in this codebase rather than the most modern available tool.

Flask over FastAPI. SQLite over Postgres. FTS5 over embeddings. Telegram over always-on mic. A shell script over a Python wrapper. The codebase already works; the goal is reliability, not resume-driven engineering.
