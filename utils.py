"""
utils.py — Shared utilities for Neo smart home hub.

IP detection is cached at module import time so it never blocks a request.
"""

import socket
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# IP detection — cached at startup
# ---------------------------------------------------------------------------

def _detect_tailscale_ip() -> str:
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=3,
        )
        ip = result.stdout.strip()
        if ip and result.returncode == 0:
            return ip
    except Exception:
        pass
    return ""


def _detect_local_ip() -> str:
    """Socket-based local IP detection (picks the interface used for outbound traffic)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.1.8"


# Cached at import time — call once, reuse everywhere
TAILSCALE_IP: str = _detect_tailscale_ip()
LOCAL_IP: str     = _detect_local_ip()
HOSTNAME: str     = socket.gethostname()


def get_tailscale_ip() -> str:
    """Return the cached Tailscale IPv4 address, or empty string if unavailable."""
    return TAILSCALE_IP


def get_local_ip() -> str:
    """Return the cached LAN IPv4 address."""
    return LOCAL_IP


# ---------------------------------------------------------------------------
# Hub startup time (for uptime reporting)
# ---------------------------------------------------------------------------

_START_TIME: float = time.monotonic()


def uptime_seconds() -> float:
    """Seconds since this module was first imported (proxy for hub startup time)."""
    return time.monotonic() - _START_TIME


# ---------------------------------------------------------------------------
# Git version
# ---------------------------------------------------------------------------

def git_version() -> str:
    """Return `git describe --tags --always` from the repo root."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=3,
            cwd=str(Path(__file__).parent),
        )
        v = result.stdout.strip()
        return v if v else "unknown"
    except Exception:
        return "unknown"
