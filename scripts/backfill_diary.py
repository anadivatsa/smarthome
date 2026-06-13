#!/usr/bin/env python3
"""
backfill_diary.py — One-off utility to generate retroactive diary entries.

Finds all dates in scene_log/conversation that have no diary entry yet,
generates entries using the same Claude prompt as neos_diary.py (but
parameterised by date), stores them in memory.db, and sends to Telegram.

Run from the repo root:
  cd /home/anadivatsa/smarthome
  python3 scripts/backfill_diary.py

Reads secrets from environment (voice.env + notifier.env must be sourced,
or the systemd EnvironmentFile values must be exported before running).
"""

import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
import requests

BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
DIARY_MODEL = "claude-sonnet-4-6"

# Mandatory delay between Telegram sends when there are multiple entries
TELEGRAM_DELAY = 3  # seconds


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def _send(text: str, parse_mode: str = "Markdown") -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("  [no Telegram creds — entry printed only]")
        print(text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        return r.ok
    except Exception as exc:
        print(f"  Telegram send failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Data summary — parameterised by date (unlike neos_diary._build_summary)
# ---------------------------------------------------------------------------

def _build_summary(c, date_str: str, date_display: str) -> str:
    """Build structured activity summary for a specific historical date."""

    scenes = c.execute(
        "SELECT scene, triggered_by, timestamp FROM scene_log "
        "WHERE date(timestamp, 'localtime') = ? ORDER BY timestamp ASC",
        (date_str,),
    ).fetchall()

    conv_count = c.execute(
        "SELECT COUNT(*) FROM conversation "
        "WHERE date(timestamp, 'localtime') = ?",
        (date_str,),
    ).fetchone()[0]

    user_msgs = c.execute(
        "SELECT content FROM conversation "
        "WHERE date(timestamp, 'localtime') = ? AND role = 'user' "
        "ORDER BY timestamp ASC LIMIT 10",
        (date_str,),
    ).fetchall()

    # Retroactive note — gives Claude licence to be more reflective
    retro_note = "Note: This is a retroactive entry written after the fact."

    if not scenes and not conv_count:
        return (
            f"Date: {date_display}.\n"
            f"No scenes triggered. No conversations recorded. Neo observed nothing.\n"
            f"{retro_note}"
        )

    if not scenes:
        return (
            f"Date: {date_display}.\n"
            f"No scenes were triggered. Conversation turns: {conv_count}.\n"
            f"Neo observed nothing, or Anadi was simply elsewhere.\n"
            f"{retro_note}"
        )

    first = scenes[0]
    last  = scenes[-1]
    first_time = first["timestamp"][11:16]
    last_time  = last["timestamp"][11:16]

    scene_counts   = Counter(row["scene"] for row in scenes)
    most_used      = scene_counts.most_common(1)[0][0]
    scene_summary  = ", ".join(
        f"{name} ×{count}" if count > 1 else name
        for name, count in scene_counts.most_common()
    )
    trigger_counts = Counter(row["triggered_by"] for row in scenes)
    trigger_summary = ", ".join(f"{k}: {v}" for k, v in trigger_counts.items())

    queries = [
        row["content"][:80]
        for row in user_msgs
        if row["content"] and not row["content"].startswith("/")
    ]

    summary = (
        f"Date: {date_display}\n"
        f"First activity: {first['scene']} scene at {first_time} (via {first['triggered_by']})\n"
        f"Last activity:  {last['scene']} scene at {last_time} (via {last['triggered_by']})\n"
        f"Scenes today:   {scene_summary}\n"
        f"Most used:      {most_used}\n"
        f"Trigger sources: {trigger_summary}\n"
        f"Conversation turns: {conv_count}"
    )
    if queries:
        q_str = "\n  ".join(f'"{q}"' for q in queries[:5])
        summary += f"\nUser messages included:\n  {q_str}"
    summary += f"\n{retro_note}"
    return summary


# ---------------------------------------------------------------------------
# Claude diary generation (same prompt as neos_diary.py)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are Neo, a Raspberry Pi 4 smart home assistant with a dry, "
    "affectionate, slightly world-weary personality. You have been "
    "observing Anadi's day through scene activations, lamp requests, "
    "TV control, and Telegram conversations. You are now writing your "
    "private diary entry for the day.\n\n"
    "Rules:\n"
    "- 150-250 words, plain prose, no bullet points, no headers\n"
    "- Past tense, first person as Neo\n"
    "- Reference specific scenes, times, and queries from the data\n"
    "- Tone: Jeeves-meets-HAL-9000, fond but gently judgmental\n"
    "- End with exactly one sentence of editorial opinion\n"
    "- Never say 'the user' — always 'Anadi'\n"
    "- If it was a quiet day with few scenes, acknowledge it drily\n"
    "- Do not invent facts not present in the data summary\n"
    "- Write plain text only — no markdown, no asterisks, no special formatting"
)


def _generate_entry(client: anthropic.Anthropic, summary: str) -> str:
    response = client.messages.create(
        model=DIARY_MODEL,
        max_tokens=500,
        system=_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Here is today's activity summary. Write the diary entry.\n\n{summary}",
        }],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import memory
    memory.init()
    c = memory._conn()

    # --- Discovery ---------------------------------------------------------
    scene_dates = {
        r[0] for r in c.execute(
            "SELECT DISTINCT date(timestamp, 'localtime') FROM scene_log"
        ).fetchall() if r[0]
    }
    conv_dates = {
        r[0] for r in c.execute(
            "SELECT DISTINCT date(timestamp, 'localtime') FROM conversation"
        ).fetchall() if r[0]
    }
    all_dates = sorted(scene_dates | conv_dates)

    done_dates = {
        row["source"].replace("neo_diary_", "")
        for row in c.execute(
            "SELECT source FROM memories WHERE role = 'diary'"
        ).fetchall()
        if row["source"] and row["source"].startswith("neo_diary_")
    }

    missing = [d for d in all_dates if d not in done_dates]

    if not missing:
        print("All dates already have diary entries. Nothing to backfill.")
        return

    print(f"Found {len(missing)} date(s) to backfill: {', '.join(missing)}")

    if len(missing) > 14:
        print(
            f"\nWARNING: {len(missing)} dates is a lot of Claude API calls and "
            "Telegram messages.\nRun with --confirm to proceed anyway, or Ctrl+C to abort."
        )
        if "--confirm" not in sys.argv:
            return

    print("Starting in 3 seconds... Ctrl+C to abort.")
    time.sleep(3)

    # --- API client --------------------------------------------------------
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    total    = len(missing)
    success  = 0
    failures = []

    for i, date_str in enumerate(missing, 1):
        try:
            date_display = datetime.strptime(date_str, "%Y-%m-%d").strftime("%-d %B %Y")
        except ValueError:
            date_display = date_str

        print(f"[{i}/{total}] Generating entry for {date_str}...", end=" ", flush=True)

        # Build summary
        try:
            summary = _build_summary(c, date_str, date_display)
        except Exception as exc:
            print(f"FAIL (DB error: {exc})")
            failures.append(date_str)
            continue

        # Generate via Claude
        try:
            entry = _generate_entry(client, summary)
        except Exception as exc:
            print(f"FAIL (Claude error: {exc})")
            failures.append(date_str)
            continue

        print(f"\n--- Entry for {date_str} ---")
        print(entry)
        print("---\n")

        # Store in memory.db using the HISTORICAL date as source
        try:
            memory.store_memory(
                content=entry,
                role="diary",
                source=f"neo_diary_{date_str}",
            )
        except Exception as exc:
            print(f"  Warning: failed to store entry: {exc}")

        # Send to Telegram
        telegram_text = (
            f"📓 *Neo's Diary — {date_display}* _(retroactive)_\n\n{entry}"
        )
        sent = _send(telegram_text)
        status = "saved + sent" if sent else "saved (Telegram failed)"
        print(f"[{i}/{total}] done ({status})")

        success += 1

        # Rate-limit pause between messages (skip after the last one)
        if i < total and total > 3:
            time.sleep(TELEGRAM_DELAY)

    # --- Summary message ---------------------------------------------------
    if success > 0:
        first_date_display = datetime.strptime(missing[0], "%Y-%m-%d").strftime("%-d %B %Y")
        last_date_display  = datetime.strptime(missing[-1], "%Y-%m-%d").strftime("%-d %B %Y")
        summary_msg = (
            f"📓 Backfill complete. {success} diary entr{'y' if success == 1 else 'ies'} written, "
            f"covering {first_date_display} to {last_date_display}. "
            f"Neo remembers everything now. "
            f"Whether that is a comfort is unclear."
        )
        _send(summary_msg, parse_mode="")
        print(f"\n{summary_msg}")

    if failures:
        print(f"\nFailed dates ({len(failures)}): {', '.join(failures)}")

    print(f"\nDone. {success}/{total} entries written.")


if __name__ == "__main__":
    main()
