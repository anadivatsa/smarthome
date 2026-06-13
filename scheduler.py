"""
scheduler.py — Lightweight task runner for Neo smart home hub.

Tasks are .py files in smarthome/tasks/. Each file has a header comment block:
  # SCHEDULE: daily at 07:30
  # ENABLED: true
  # DESCRIPTION: Some task description

Supported schedule expressions:
  daily at HH:MM
  weekly on <weekday> at HH:MM   (monday/tuesday/.../sunday)

Failed tasks are logged. A task that fails 3 consecutive times is auto-disabled
by rewriting its ENABLED header to false.

Start as a daemon background thread from hub.py: import scheduler; scheduler.start()
"""

import logging
import re
import subprocess
import sys
import threading
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("scheduler")

_BASE      = Path(__file__).parent
_TASKS_DIR = _BASE / "tasks"
_scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
_fail_counts: dict[str, int] = {}   # task_name → consecutive failure count
_MAX_FAILS = 3

# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------

def _parse_header(path: Path) -> dict:
    """Extract SCHEDULE, ENABLED, DESCRIPTION from task file header."""
    meta = {"schedule": None, "enabled": False, "description": ""}
    try:
        text = path.read_text()
        for line in text.splitlines()[:20]:
            line = line.strip()
            if not line.startswith("#"):
                break
            m = re.match(r"#\s*SCHEDULE\s*:\s*(.+)", line, re.IGNORECASE)
            if m:
                meta["schedule"] = m.group(1).strip()
            m = re.match(r"#\s*ENABLED\s*:\s*(.+)", line, re.IGNORECASE)
            if m:
                meta["enabled"] = m.group(1).strip().lower() == "true"
            m = re.match(r"#\s*DESCRIPTION\s*:\s*(.+)", line, re.IGNORECASE)
            if m:
                meta["description"] = m.group(1).strip()
    except Exception as exc:
        log.warning("scheduler: could not parse header of %s: %s", path.name, exc)
    return meta


def _parse_schedule(expr: str | None) -> CronTrigger | None:
    """Convert schedule expression string to APScheduler CronTrigger."""
    if not expr:
        return None
    expr = expr.strip().lower()
    # "daily at HH:MM"
    m = re.match(r"daily\s+at\s+(\d{1,2}):(\d{2})", expr)
    if m:
        return CronTrigger(hour=int(m.group(1)), minute=int(m.group(2)))
    # "weekly on <weekday> at HH:MM"
    days = {"monday": "mon", "tuesday": "tue", "wednesday": "wed",
            "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"}
    m = re.match(r"weekly\s+on\s+(\w+)\s+at\s+(\d{1,2}):(\d{2})", expr)
    if m:
        day = days.get(m.group(1))
        if day:
            return CronTrigger(day_of_week=day, hour=int(m.group(2)), minute=int(m.group(3)))
    log.warning("scheduler: unrecognised schedule expression: %r", expr)
    return None


# ---------------------------------------------------------------------------
# Task runner
# ---------------------------------------------------------------------------

def _disable_task(path: Path) -> None:
    """Rewrite ENABLED: true → false in the task file header."""
    try:
        text = path.read_text()
        new  = re.sub(
            r"(#\s*ENABLED\s*:\s*)true",
            r"\1false",
            text,
            flags=re.IGNORECASE,
        )
        path.write_text(new)
        log.warning("scheduler: auto-disabled %s after %d consecutive failures", path.name, _MAX_FAILS)
    except Exception as exc:
        log.error("scheduler: could not disable %s: %s", path.name, exc)


def _run_task(path: Path) -> None:
    """Execute one task file as a subprocess."""
    name = path.stem
    log.info("scheduler: running task %s", name)
    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            _fail_counts[name] = 0
            log.info("scheduler: task %s completed OK", name)
        else:
            _fail_counts[name] = _fail_counts.get(name, 0) + 1
            log.error("scheduler: task %s failed (exit %d):\n%s",
                      name, result.returncode, (result.stdout + result.stderr)[:500])
            if _fail_counts[name] >= _MAX_FAILS:
                _disable_task(path)
                _fail_counts[name] = 0
    except subprocess.TimeoutExpired:
        _fail_counts[name] = _fail_counts.get(name, 0) + 1
        log.error("scheduler: task %s timed out", name)
    except Exception as exc:
        _fail_counts[name] = _fail_counts.get(name, 0) + 1
        log.error("scheduler: task %s error: %s", name, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start() -> None:
    """Load tasks from tasks/ and start the scheduler background thread."""
    _TASKS_DIR.mkdir(exist_ok=True)
    tasks = list(_TASKS_DIR.glob("*.py"))
    loaded = 0
    for path in sorted(tasks):
        if path.name.startswith("_"):
            continue
        meta = _parse_header(path)
        if not meta["enabled"]:
            log.debug("scheduler: %s disabled, skipping", path.name)
            continue
        if not meta["schedule"]:
            log.warning("scheduler: %s has no SCHEDULE header, skipping", path.name)
            continue
        trigger = _parse_schedule(meta["schedule"])
        if trigger is None:
            continue
        _scheduler.add_job(
            _run_task,
            trigger=trigger,
            args=[path],
            id=path.stem,
            replace_existing=True,
            name=meta["description"] or path.stem,
        )
        log.info("scheduler: loaded %s → %s", path.name, meta["schedule"])
        loaded += 1

    _scheduler.start()
    log.info("scheduler: started with %d task(s)", loaded)


def stop() -> None:
    """Gracefully shut down the scheduler."""
    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        pass
