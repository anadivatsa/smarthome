#!/usr/bin/env python3
# SCHEDULE: daily at 23:00
# ENABLED: true
# DESCRIPTION: Neo writes a wry first-person diary entry about the day

"""
Neo's Diary — nightly AI diary entry written from Neo's perspective.

Pulls today's scene activations and conversation data from memory.db,
sends to Claude Sonnet for a witty first-person entry, stores the result
in memory.db and delivers it to Telegram.
"""

import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
import requests

BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
DIARY_MODEL = "claude-sonnet-4-6"


def _send(text: str, parse_mode: str = "Markdown") -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing to stdout:")
        print(text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
    except Exception as exc:
        print(f"Telegram send failed: {exc}")


def _send_error() -> None:
    _send(
        "Neo is unable to write today. Perhaps some things are better left unrecorded.",
        parse_mode="",
    )


def _build_summary(c, date_str: str, date_display: str) -> str:
    """Query memory.db and return a structured data summary string for Claude."""

    # Scene activations today
    scenes = c.execute(
        "SELECT scene, triggered_by, timestamp FROM scene_log "
        "WHERE date(timestamp, 'localtime') = date('now', 'localtime') "
        "ORDER BY timestamp ASC"
    ).fetchall()

    # Conversation turns today
    conv_count = c.execute(
        "SELECT COUNT(*) FROM conversation "
        "WHERE date(timestamp, 'localtime') = date('now', 'localtime')"
    ).fetchone()[0]

    # User messages today (non-command, for flavour)
    user_msgs = c.execute(
        "SELECT content FROM conversation "
        "WHERE date(timestamp, 'localtime') = date('now', 'localtime') "
        "AND role = 'user' "
        "ORDER BY timestamp ASC LIMIT 10"
    ).fetchall()

    # Is this the very first diary entry ever?
    diary_count = c.execute(
        "SELECT COUNT(*) FROM memories WHERE role = 'diary'"
    ).fetchone()[0]

    first_entry_note = "(Note: this is the first ever diary entry.)" if diary_count == 0 else ""

    if not scenes:
        summary = (
            f"Date: {date_display}.\n"
            f"No scenes were triggered today. Conversation turns: {conv_count}.\n"
            f"Neo observed nothing, or Anadi was simply elsewhere."
        )
        if first_entry_note:
            summary += f"\n{first_entry_note}"
        return summary

    # Temporal bookends
    first = scenes[0]
    last  = scenes[-1]
    first_time = first["timestamp"][11:16]
    last_time  = last["timestamp"][11:16]

    # Scene frequency
    scene_counts = Counter(row["scene"] for row in scenes)
    most_used = scene_counts.most_common(1)[0][0]
    scene_summary = ", ".join(
        f"{name} ×{count}" if count > 1 else name
        for name, count in scene_counts.most_common()
    )

    # How scenes were triggered
    trigger_counts = Counter(row["triggered_by"] for row in scenes)
    trigger_summary = ", ".join(f"{k}: {v}" for k, v in trigger_counts.items())

    # Interesting user messages (skip slash-commands)
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
        f"Conversation turns today: {conv_count}"
    )

    if queries:
        q_str = "\n  ".join(f'"{q}"' for q in queries[:5])
        summary += f"\nUser messages today included:\n  {q_str}"

    if first_entry_note:
        summary += f"\n{first_entry_note}"

    return summary


def run() -> None:
    import memory
    memory.init()

    now = datetime.now()
    date_str     = now.strftime("%Y-%m-%d")
    date_display = now.strftime("%-d %B %Y")

    # Gather today's data
    try:
        c       = memory._conn()
        summary = _build_summary(c, date_str, date_display)
    except Exception as exc:
        print(f"DB query failed: {exc}")
        _send_error()
        return

    print(f"Data summary:\n{summary}\n")

    # Ask Claude to write the entry
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ANTHROPIC_API_KEY not set")
        _send_error()
        return

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=DIARY_MODEL,
            max_tokens=500,
            system=(
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
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Here is today's activity summary. Write the diary entry.\n\n"
                    f"{summary}"
                ),
            }],
        )
        diary_entry = response.content[0].text.strip()
    except Exception as exc:
        print(f"Claude API failed: {exc}")
        _send_error()
        return

    print(f"\nDiary entry:\n{diary_entry}\n")

    # Persist to memory.db
    try:
        memory.store_memory(
            content=diary_entry,
            role="diary",
            source=f"neo_diary_{date_str}",
        )
    except Exception as exc:
        print(f"Warning: failed to store diary entry: {exc}")
        # Non-fatal — still send to Telegram

    # Send to Telegram
    telegram_text = f"📓 *Neo's Diary — {date_display}*\n\n{diary_entry}"
    _send(telegram_text)
    print(f"Diary entry written and sent for {date_str}")


if __name__ == "__main__":
    run()
