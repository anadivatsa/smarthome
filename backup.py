"""
backup.py — Timestamped file backup utility for Neo.

backup_file(path) copies the file to data/backups/<name>_YYYYMMDD_HHMMSS<ext>
and returns the backup path. Called before any automated file overwrite (e.g.
the /repair self-fix flow in tgvoice.py).
"""

import shutil
from datetime import datetime
from pathlib import Path

_BASE    = Path(__file__).parent
_BACKUPS = _BASE / "data" / "backups"


def backup_file(path: str | Path) -> Path:
    """
    Copy *path* to data/backups/ with a timestamp suffix.
    Returns the backup Path. Never raises — logs and returns None on failure.
    """
    _BACKUPS.mkdir(parents=True, exist_ok=True)
    src = Path(path)
    if not src.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest  = _BACKUPS / f"{src.stem}_{stamp}{src.suffix}"
    try:
        shutil.copy2(src, dest)
        return dest
    except Exception as exc:
        import logging
        logging.getLogger("backup").warning("backup_file failed for %s: %s", path, exc)
        return None
