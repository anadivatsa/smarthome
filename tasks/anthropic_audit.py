#!/usr/bin/env python3
# SCHEDULE: daily at 03:00
# ENABLED: false
# DESCRIPTION: Anthropic Audit — 72h behavioral drift detection (auto-enabled after --baseline)
"""
Thin launcher for audit.py. Spawns it in the background so this task
returns immediately — audit runs take several minutes (30 API calls + 2
comparison calls) which would exceed the scheduler's 120s timeout.
audit.py enforces its own 72h interval check internally.
"""

import subprocess
import sys
from pathlib import Path

audit_py = Path(__file__).parent.parent / "audit.py"

proc = subprocess.Popen(
    [sys.executable, str(audit_py)],
    start_new_session=True,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(f"Anthropic Audit started in background (pid {proc.pid}).")
