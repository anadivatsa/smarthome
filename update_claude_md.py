#!/usr/bin/env python3
"""
update_claude_md.py — Auto-sync CLAUDE.md with current codebase state.
Installed as a cron job running every 30 minutes.
Skips the API call entirely if nothing has changed since the last update.
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SMARTHOME = Path(__file__).parent
CLAUDE_MD = SMARTHOME / "CLAUDE.md"
LOG = SMARTHOME / "claude_md_update.log"


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with LOG.open("a") as f:
        f.write(line + "\n")


def _git(*args) -> str:
    r = subprocess.run(["git", "-C", str(SMARTHOME)] + list(args),
                       capture_output=True, text=True)
    return r.stdout.strip()


def _service_status(name: str) -> str:
    r = subprocess.run(["systemctl", "is-active", f"{name}.service"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def _has_changes() -> bool:
    """Return True if any .py or .json file is newer than CLAUDE.md."""
    if not CLAUDE_MD.exists():
        return True
    md_mtime = CLAUDE_MD.stat().st_mtime
    candidates = list(SMARTHOME.glob("*.py")) + list(SMARTHOME.glob("*.json"))
    return any(f.stat().st_mtime > md_mtime for f in candidates if f.exists())


def _load_api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        env_path = SMARTHOME / "voice.env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return key


def main():
    if not _has_changes():
        _log("No changes detected — skipping.")
        return

    current_md = CLAUDE_MD.read_text() if CLAUDE_MD.exists() else ""

    recent_commits = _git("log", "--oneline", "-8")
    recent_diff    = _git("diff", "HEAD~1", "HEAD", "--stat")
    git_status     = _git("status", "--short")
    py_files       = sorted(f.name for f in SMARTHOME.glob("*.py"))

    services = {
        svc: _service_status(svc)
        for svc in ["hub", "wiz-lamp", "voice", "tgvoice", "bt_jbl"]
    }
    service_block = "\n".join(f"  {k}: {v}" for k, v in services.items())

    key = _load_api_key()
    if not key:
        _log("ANTHROPIC_API_KEY not found — skipping.")
        return

    import anthropic
    client = anthropic.Anthropic(api_key=key)

    today = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""You are maintaining CLAUDE.md for a Raspberry Pi smart home project (hostname: Neo).

Current CLAUDE.md:
{current_md}

---
Recent git commits:
{recent_commits or "none"}

Last commit diff stat:
{recent_diff or "none"}

Working tree status:
{git_status or "clean"}

Python files currently in smarthome/:
{", ".join(py_files)}

Live service statuses:
{service_block}

Today's date: {today}
---

Task: Return the COMPLETE updated CLAUDE.md — nothing else, no preamble, no code fences.

Rules:
- Update the "Last updated" header line to {today} with a short description of what changed.
- Add any new .py files to the File Layout section if they are missing.
- Update the Services table if any status changed.
- Update the Roadmap if a stage was completed or a new one started.
- Keep everything else exactly as-is.
- Do not speculate or add placeholder content.
- If nothing meaningful changed, return the file with only the date updated."""

    _log("Calling Claude to update CLAUDE.md…")
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    updated = msg.content[0].text.strip()

    # Strip accidental code fences
    if updated.startswith("```"):
        updated = updated.split("```")[1]
        if updated.startswith("markdown") or updated.startswith("md"):
            updated = updated.split("\n", 1)[1]
        updated = updated.rsplit("```", 1)[0].strip()

    if len(updated) < 500:
        _log(f"Output suspiciously short ({len(updated)} chars) — skipping write.")
        return

    CLAUDE_MD.write_text(updated + "\n")
    _log(f"CLAUDE.md updated ({len(updated)} chars).")


if __name__ == "__main__":
    main()
