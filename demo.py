#!/usr/bin/env python3
"""
Grand Lamp Demo — the full tour, live.
"""
import requests, time, sys

BASE = "http://localhost:5000"

def lamp(ep):
    try:
        r = requests.get(f"{BASE}/{ep}", timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def act(ep, title, description, hold):
    print(f"\n{'─'*55}")
    print(f"  {title}")
    print(f"  {description}")
    lamp(ep)
    sys.stdout.flush()
    time.sleep(hold)

# ── Overture ──────────────────────────────────────────────
lamp("off")
print("\n" + "═"*55)
print("  GRAND LAMP DEMO  —  live right now")
print("═"*55)
time.sleep(1)

# ── Act 1: Power ──────────────────────────────────────────
act("on",      "⚡ ON",
    "Full blast. 255 brightness, pure white.", 2)

act("focus",   "🔬 FOCUS",
    "6500K — cool daylight. Harshest it gets.", 3)

act("reading", "📖 READING",
    "4000K, 80% — neutral, easy on eyes.", 3)

act("morning", "🌤  MORNING",
    "5000K, 70% — crisp daylight.", 3)

# ── Act 2: Warmth ─────────────────────────────────────────
act("dinner",  "🍽  DINNER",
    "2500K, 50% — candlelight table.", 3)

act("relax",   "🛋  RELAX",
    "2400K, 40% — soft amber, Sunday evening.", 3)

act("movie",   "🎬 MOVIE",
    "2700K, 30% — cinema warm, eyes comfortable.", 3)

act("romance", "❤️  ROMANCE",
    "Deep red-warm RGB, 15% — barely lit.", 3)

act("sleep",   "😴 SLEEP",
    "2200K, 10% — almost nothing. Whispering warm.", 3)

# ── Act 3: Fire ───────────────────────────────────────────
act("candle",  "🕯  CANDLE",
    "Warm RGB flicker — random brightness 60–160, 0.05–0.18s intervals.", 7)

act("campfire","🔥 CAMPFIRE",
    "Wilder than candle — brightness 40–220, faster chaos.", 7)

# ── Act 4: Atmosphere ─────────────────────────────────────
act("pulse",   "💓 PULSE",
    "Slow breathing — 3000K cycling 20→255→20 brightness.", 10)

act("aurora",  "🌌 AURORA",
    "Northern lights — green→indigo→violet→cyan, 12s each leg.", 24)

# ── Act 5: Chaos ──────────────────────────────────────────
act("party",   "🎉 PARTY",
    "9-colour random burst — 0.4–0.9s per colour, full saturation.", 9)

act("blink",   "💡 BLINK",
    "Fast on/off — 400ms intervals, 4000K white.", 5)

act("strobe",  "⚡ STROBE",
    "Full 6500K white strobe — 80ms on/off. Don't look directly.", 4)

# ── Act 6: Danger ─────────────────────────────────────────
act("alert",   "🚨 ALERT  ·  ·  ·  —  —  —  ·  ·  ·",
    "Red SOS morse — 150ms short / 450ms long, looping.", 12)

# ── Act 7: Transitions (the slow cinema) ──────────────────
lamp("off")
print(f"\n{'─'*55}")
print("  ⏸  DARKNESS  —  clearing the palette")
time.sleep(2)

act("wake",    "🌅 WAKE",
    "90s ramp: 2200K dim → 6500K full daylight. Watch it climb.", 92)

act("bedtime", "🌙 BEDTIME",
    "2-min warm dim-down: 4000K → 2200K, 200 → 25 brightness.", 40)

# ── Grand Finale ──────────────────────────────────────────
lamp("off")
print(f"\n{'─'*55}")
print("  🌆  GRAND FINALE — SUNSET")
print("  5.5 minutes: golden → orange → deep red → darkness")
print("  Sit back.")
lamp("sunset")
print(f"{'─'*55}")
print("\n  Running. Lamp will go dark on its own.")
print("  Hit /off any time to stop.")
print("\n" + "═"*55)
