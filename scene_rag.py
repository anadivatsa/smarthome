#!/usr/bin/env python3
"""
scene_rag.py — Semantic scene selection via a minimal RAG pipeline.

Pipeline:
  1. RETRIEVAL  — TF-IDF cosine similarity scores all 18 scenes against the
                  query at startup. Pure Python, no API call, no extra deps.
  2. GENERATION — Claude receives only the top-k candidates and picks the best
                  one, returning a natural-language confirmation reply.

This is RAG in miniature:
  - The knowledge base  (SCENE_KB)  is the "document corpus"
  - retrieve()          is the "R"  — find relevant docs via similarity search
  - generate()          is the "G"  — LLM synthesises an answer from retrieved docs

Run as CLI:
  python scene_rag.py "something cozy for a cold evening"
  python scene_rag.py "I want to dance"

Import and call from other modules:
  from scene_rag import run
  result = run("something cozy")
  # {"scene": "relax", "reply": "Starting relax...", "candidates": [...]}
"""

import json
import math
import os
import re
import sys
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# Knowledge base — richer descriptions than scenes.json carries.
# These are the "documents" that get indexed and retrieved.
# ---------------------------------------------------------------------------

SCENE_KB: dict[str, dict[str, str]] = {
    "movie": {
        "description": "Cinema-like atmosphere for watching films or TV. Warm dim lighting.",
        "tags": "film cinema movie watching dark dim cozy evening entertainment streaming video",
    },
    "netflix": {
        "description": "Netflix streaming mode. Dim warm light, Netflix app launches automatically.",
        "tags": "netflix streaming show series binge watching dim cozy dark",
    },
    "youtube": {
        "description": "YouTube browsing. Relaxed ambient lighting for casual video watching.",
        "tags": "youtube video casual browsing ambient relaxed comfortable",
    },
    "youtube-music": {
        "description": "Music listening via YouTube playlist. Energised light, music auto-starts.",
        "tags": "music youtube playlist energised morning bright listening background",
    },
    "focus": {
        "description": "Work and study mode. Bright cool white light, TV off. Maximum concentration.",
        "tags": "work study focus concentration bright cool white productive office daytime task",
    },
    "relax": {
        "description": "Cozy relaxation. Soft warm amber light, TV on low. Perfect evening wind-down.",
        "tags": "relax cozy warm amber soft evening wind-down chill comfortable cold quiet calm",
    },
    "goodnight": {
        "description": "Bedtime wind-down. Lamp fades slowly over 5 minutes, TV off. Sleep prep.",
        "tags": "sleep bedtime goodnight fade tired night dark rest dreaming",
    },
    "morning": {
        "description": "Gentle wake-up. Lamp gradually brightens over 90 seconds, YouTube starts quietly.",
        "tags": "morning wake up sunrise gradual bright gentle start day fresh",
    },
    "party": {
        "description": "Party mode. Colour-cycling lamp, Spotify on loud, TV on. High-energy celebration.",
        "tags": "party celebration fun energetic dancing dance music colours lights loud vibrant hype",
    },
    "off": {
        "description": "Everything off. Lamp off, TV off. Total silence and darkness.",
        "tags": "off dark silence everything quiet done leaving stop night",
    },
    "sunset": {
        "description": "Slow sunset transition. Golden to deep red to off over 5 minutes. Peaceful.",
        "tags": "sunset dusk golden hour fade transition peaceful calm evening end day orange",
    },
    "dinner": {
        "description": "Dinner ambiance. Candlelight-warm low light, TV off. Intimate meal atmosphere.",
        "tags": "dinner eating meal food candlelight warm romantic intimate ambiance cozy table",
    },
    "gaming": {
        "description": "Gaming session. Blue-white accent lighting, TV on high volume.",
        "tags": "gaming game play controller blue white bright tv competitive intense focus screen",
    },
    "romance": {
        "description": "Romantic atmosphere. Deep red very dim lighting, TV off. Intimate mood.",
        "tags": "romance romantic red intimate passion love date night mood low dim seductive",
    },
    "reading": {
        "description": "Reading light. Neutral white 80% brightness, TV off. Easy on the eyes.",
        "tags": "reading book neutral white comfortable eye strain quiet calm concentration",
    },
    "music": {
        "description": "Music listening session. Lamp pulses to the beat, Spotify auto-starts.",
        "tags": "music spotify listening pulse rhythm beat groove audio sound chill enjoyment",
    },
    "leave": {
        "description": "Leaving home. Everything shuts off and presence is set to away.",
        "tags": "leaving home away going out exit departure bye off lock up",
    },
    "thunderstruck": {
        "description": "THUNDERSTRUCK. Pure blue lamp, AC/DC at full volume. Maximum rock energy.",
        "tags": "rock acdc loud electric guitar thunderstruck blue energy intense hype headbang",
    },
}

# ---------------------------------------------------------------------------
# Retrieval — TF-IDF index built once at import time (no API call)
# ---------------------------------------------------------------------------

def _tokenise(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def _build_index() -> dict[str, dict[str, float]]:
    corpus = {
        name: _tokenise(f"{data['description']} {data['tags']}")
        for name, data in SCENE_KB.items()
    }
    N = len(corpus)
    df: dict[str, int] = {}
    for tokens in corpus.values():
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1

    index: dict[str, dict[str, float]] = {}
    for name, tokens in corpus.items():
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        total = len(tokens)
        tfidf: dict[str, float] = {}
        for term, count in tf.items():
            idf = math.log(N / df[term])
            tfidf[term] = (count / total) * idf
        index[name] = tfidf
    return index


_INDEX = _build_index()


def _cosine(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    dot = sum(vec_a.get(t, 0.0) * vec_b.get(t, 0.0) for t in vec_b)
    mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
    mag_b = math.sqrt(sum(v * v for v in vec_b.values()))
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0


def retrieve(query: str, n: int = 3) -> list[tuple[str, float]]:
    """
    Retrieval step (the R in RAG).
    Returns top-n (scene_name, similarity_score) pairs for the query.
    No API call — pure TF-IDF cosine similarity.
    """
    q_tokens = _tokenise(query)
    if not q_tokens:
        return []
    q_tf: dict[str, float] = {t: 1.0 / len(q_tokens) for t in q_tokens}
    scores = [(name, _cosine(q_tf, vec)) for name, vec in _INDEX.items()]
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:n]


# ---------------------------------------------------------------------------
# Generation — Claude picks from retrieved candidates (the G in RAG)
# ---------------------------------------------------------------------------

_claude: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _claude
    if _claude is None:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _claude = anthropic.Anthropic(api_key=key)
    return _claude


def generate(query: str, candidates: list[tuple[str, float]]) -> dict:
    """
    Generation step (the G in RAG).
    Claude receives only the retrieved candidates — not all 18 scenes.
    This is what makes it RAG: the context passed to the LLM is grounded
    in what the retrieval step found relevant.

    Returns: {"scene": str | None, "reply": str}
    """
    if not candidates:
        return {"scene": None, "reply": "No matching scene found."}

    candidate_block = "\n".join(
        f"- {name}: {SCENE_KB[name]['description']}  (retrieval score: {score:.3f})"
        for name, score in candidates
    )

    prompt = (
        f'The user said: "{query}"\n\n'
        f"Retrieved scene candidates (ranked by TF-IDF similarity):\n{candidate_block}\n\n"
        "Pick the single best scene. Reply with JSON only — no markdown, no prose:\n"
        '{"scene": "<name>", "reply": "<one natural sentence confirming the choice>"}\n\n'
        "If none fit, return: "
        '{"scene": null, "reply": "I couldn\'t find a matching scene for that."}'
    )

    msg = _get_client().messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"scene": None, "reply": raw}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(query: str, top_k: int = 3) -> dict:
    """
    Full RAG pipeline in two steps:
      1. retrieve() — TF-IDF scores all scenes, returns top_k (no API)
      2. generate() — Claude picks best from those candidates (one API call)

    Returns:
      {
        "scene":      str | None,   # scene name to trigger, or None
        "reply":      str,          # natural-language confirmation
        "candidates": [(name, score), ...]
      }
    """
    candidates = retrieve(query, n=top_k)
    result = generate(query, candidates)
    result["candidates"] = candidates
    return result


# ---------------------------------------------------------------------------
# CLI — test the pipeline from the terminal
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scene_rag.py \"your natural language scene request\"")
        print("Example: python scene_rag.py \"something cozy for a cold evening\"")
        sys.exit(1)

    query = " ".join(sys.argv[1:])

    # Load ANTHROPIC_API_KEY from voice.env if not already in environment
    env_path = Path(__file__).parent / "voice.env"
    if env_path.exists() and not os.getenv("ANTHROPIC_API_KEY"):
        for line in env_path.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()
                break

    print(f'\nQuery: "{query}"')

    print("\n── Retrieval (TF-IDF, no API) ──────────────────────────")
    candidates = retrieve(query)
    for name, score in candidates:
        bar = "█" * int(score * 200)
        print(f"  {name:<20}  {score:.4f}  {bar}  {SCENE_KB[name]['description'][:55]}")

    print("\n── Generation (Claude) ─────────────────────────────────")
    result = generate(query, candidates)
    print(f"  Scene : {result.get('scene')}")
    print(f"  Reply : {result.get('reply')}")
