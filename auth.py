"""
auth.py — API key authentication for the Neo hub.

Key is read from NEO_API_KEY (set in hub.env, loaded by hub.service).
If the variable is empty the hub runs in open mode — all requests allowed.
This makes first-time setup safe: set the key, restart hub, then lock down.

Clients send the key via:
  Header:      X-Neo-Key: <key>          ← preferred (tgvoice, voice, dashboard)
  Query param: ?key=<key>                ← Siri Shortcuts fallback (URL-only clients)

Public paths — never require a key:
  /                   dashboard HTML
  /spotify/auth       OAuth initiation (user visits manually)
  /spotify/callback   Spotify OAuth redirect
  /spotify/exchange   Headless code exchange
  /shortcuts          Siri reference page (read-only)
"""
import os
import secrets

from flask import jsonify, request

_PUBLIC = frozenset([
    "/",
    "/spotify/auth",
    "/spotify/callback",
    "/spotify/exchange",
    "/shortcuts",
])


def _configured_key() -> str:
    return os.getenv("NEO_API_KEY", "").strip()


def check_auth():
    """
    Called from Flask before_request.
    Returns None to continue, or a (Response, 401) tuple to reject.
    """
    key = _configured_key()
    if not key:
        return None  # open mode — no key configured yet

    if request.path in _PUBLIC:
        return None  # always public

    provided = (
        request.headers.get("X-Neo-Key", "").strip()
        or request.args.get("key", "").strip()
    )
    if not provided:
        return (
            jsonify({
                "error": "Unauthorized",
                "hint": "Send X-Neo-Key header or append ?key=<NEO_API_KEY> to the URL",
            }),
            401,
        )
    # Constant-time comparison — prevents timing-based key enumeration
    if not secrets.compare_digest(provided.encode(), key.encode()):
        return jsonify({"error": "Unauthorized"}), 401
    return None
