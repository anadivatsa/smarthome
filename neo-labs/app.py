#!/usr/bin/env python3
import json
import logging
import os

import anthropic
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
PORT  = int(os.getenv("NEO_LABS_PORT", 3000))
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [neo-labs] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("neo-labs")

_client = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = anthropic.Anthropic(api_key=key)
    return _client


def ask(system: str, user: str) -> dict:
    msg = get_client().messages.create(
        model=MODEL,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Settle This
# ---------------------------------------------------------------------------

SETTLE_SYSTEM = (
    'You are a ruthlessly impartial judge. Read both sides of an argument and deliver a clear, '
    'firm verdict. No fence-sitting. Pick a winner and explain why concisely. '
    'Respond in strict JSON only — no markdown, no prose outside the JSON: '
    '{"winner": "A or B or Draw", "verdict": "one punchy sentence", '
    '"reasoning": "2-3 sentences", "caveat": "one thing the loser got right, or null"}'
)


@app.route("/api/settle", methods=["POST"])
def settle():
    data = request.get_json(force=True)
    side_a = data.get("side_a", "").strip()
    side_b = data.get("side_b", "").strip()
    if not side_a or not side_b:
        return jsonify({"error": "Both sides required"}), 400
    try:
        result = ask(SETTLE_SYSTEM, f"Side A: {side_a}\n\nSide B: {side_b}")
        log.info("settle → winner=%s", result.get("winner"))
        return jsonify(result)
    except Exception as exc:
        log.error("settle error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Grade My Bit
# ---------------------------------------------------------------------------

GRADE_SYSTEM = (
    'You are a veteran stand-up comedy coach. Grade the joke on three dimensions: '
    'Setup (clarity and premise), Subversion (does it go somewhere unexpected?), '
    'Punchline (delivery and payoff). Score each 1-10. Give a short note per dimension. '
    'Give an overall score (average, one decimal) and a one-line verdict. '
    'Write a rewrite that improves the weakest dimension. '
    'Respond in strict JSON only — no markdown, no prose outside the JSON: '
    '{"setup": {"score": n, "note": "..."}, "subversion": {"score": n, "note": "..."}, '
    '"punchline": {"score": n, "note": "..."}, "overall": n, "verdict": "...", "rewrite": "..."}'
)


@app.route("/api/grade", methods=["POST"])
def grade():
    data = request.get_json(force=True)
    joke = data.get("joke", "").strip()
    if not joke:
        return jsonify({"error": "Joke required"}), 400
    try:
        result = ask(GRADE_SYSTEM, f"Joke: {joke}")
        log.info("grade → overall=%.1f", result.get("overall", 0))
        return jsonify(result)
    except Exception as exc:
        log.error("grade error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# City Wars
# ---------------------------------------------------------------------------

CITY_SYSTEM = (
    'You are a witty, sharp urban critic. Given a city, produce: '
    '(1) roast — a punchy, affectionate 3-sentence roast of the city\'s quirks and contradictions; '
    '(2) traits — exactly 4 short traits that describe the city like a person. '
    'Be funny but not cruel. '
    'Respond in strict JSON only — no markdown, no prose outside the JSON: '
    '{"roast": "...", "traits": ["...", "...", "...", "..."]}'
)

RIVALRY_SYSTEM = (
    'You are a witty urban rivalry commentator. Given two cities, write a head-to-head roast battle. '
    'Each city gets a 2-sentence roast targeting the other\'s quirks. '
    'Declare a winner based on which city\'s personality wins the matchup (style, energy, attitude). '
    'Respond in strict JSON only — no markdown, no prose outside the JSON: '
    '{"city_a_roast": "...", "city_b_roast": "...", "winner": "city name", "winner_reason": "one sentence"}'
)


@app.route("/api/city", methods=["POST"])
def city():
    data = request.get_json(force=True)
    city_name = data.get("city", "").strip()
    if not city_name:
        return jsonify({"error": "City required"}), 400
    try:
        result = ask(CITY_SYSTEM, f"City: {city_name}")
        log.info("city → %s", city_name)
        return jsonify(result)
    except Exception as exc:
        log.error("city error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/city/rivalry", methods=["POST"])
def city_rivalry():
    data = request.get_json(force=True)
    city_a = data.get("city_a", "").strip()
    city_b = data.get("city_b", "").strip()
    if not city_a or not city_b:
        return jsonify({"error": "Both cities required"}), 400
    try:
        result = ask(RIVALRY_SYSTEM, f"City A: {city_a}\nCity B: {city_b}")
        log.info("rivalry → %s vs %s → winner=%s", city_a, city_b, result.get("winner"))
        return jsonify(result)
    except Exception as exc:
        log.error("rivalry error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Negotiate This
# ---------------------------------------------------------------------------

NEGOTIATE_SYSTEM = (
    'You are a no-nonsense salary negotiation coach specialising in the Indian job market. '
    'Given job offer details, return: '
    '(1) opening_line — the exact words to say when countering (natural, confident, not aggressive — '
    'something you\'d actually say on a call); '
    '(2) ask_number — the specific annual CTC to ask for in INR (be concrete — one number, not a range); '
    '(3) rationale — 2 sentences on why this number is justified given the role and market; '
    '(4) fallback — the floor CTC to accept if pushed hard. '
    'Be India-specific: account for Tier-1 vs Tier-2 city costs, typical hike norms, '
    'startup vs MNC expectations, and cultural comfort with negotiation. '
    'Respond in strict JSON only — no markdown, no prose outside the JSON: '
    '{"opening_line": "...", "ask_number": "₹X LPA", "rationale": "...", "fallback": "₹X LPA"}'
)


@app.route("/api/negotiate", methods=["POST"])
def negotiate():
    data = request.get_json(force=True)
    offer = data.get("offer_details", "").strip()
    if not offer:
        return jsonify({"error": "Offer details required"}), 400
    try:
        result = ask(NEGOTIATE_SYSTEM, f"Offer details: {offer}")
        log.info("negotiate → ask=%s", result.get("ask_number"))
        return jsonify(result)
    except Exception as exc:
        log.error("negotiate error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        raise SystemExit("ANTHROPIC_API_KEY not set — add it to voice.env")
    log.info("Neo Labs starting on port %d (model: %s)", PORT, MODEL)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
