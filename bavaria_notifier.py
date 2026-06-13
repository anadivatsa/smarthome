import os
import requests
import time
import json
from datetime import datetime, timezone
from pathlib import Path

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BAYERN_ID = 40
API_BASE = "https://api.openligadb.de"

STATE_FILE = str(Path.home() / ".bayern_state.json")

def send(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_matches():
    try:
        r = requests.get(f"{API_BASE}/getmatchdata/bl1", timeout=10)
        return r.json()
    except Exception:
        return []

def find_bayern_match(matches):
    for m in matches:
        if m["team1"]["teamId"] == BAYERN_ID or m["team2"]["teamId"] == BAYERN_ID:
            return m
    return None

def get_match(match_id):
    try:
        r = requests.get(f"{API_BASE}/getmatchdata/{match_id}", timeout=10)
        return r.json()
    except Exception:
        return None

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def format_score(match):
    t1 = match["team1"]["teamName"]
    t2 = match["team2"]["teamName"]
    results = match.get("matchResults", [])
    s1, s2 = 0, 0
    for r in results:
        if r["resultTypeID"] == 2:  # final/current score
            s1, s2 = r["pointsTeam1"], r["pointsTeam2"]
            break
        elif r["resultTypeID"] == 1:  # half time
            s1, s2 = r["pointsTeam1"], r["pointsTeam2"]
    return f"{t1} {s1} - {s2} {t2}"

def get_score_tuple(match):
    for r in match.get("matchResults", []):
        if r["resultTypeID"] == 2:
            return r["pointsTeam1"], r["pointsTeam2"]
    for r in match.get("matchResults", []):
        if r["resultTypeID"] == 1:
            return r["pointsTeam1"], r["pointsTeam2"]
    return 0, 0

def is_live(match):
    if match.get("matchIsFinished"):
        return False
    dt_str = match.get("matchDateTimeUTC", "")
    if not dt_str:
        return False
    try:
        # Remove trailing Z if present and parse
        dt_str = dt_str.replace("Z", "+00:00")
        match_time = datetime.fromisoformat(dt_str)
        now = datetime.now(timezone.utc)
        elapsed = (now - match_time).total_seconds()
        return 0 <= elapsed <= 7200  # within 2 hours of kickoff
    except Exception:
        return False

def main():
    print("Bayern Munich Notifier started...")
    send("🔴⚽ <b>Bayern Munich Notifier started!</b>\nYou'll get updates for every Bundesliga match.")

    state = load_state()
    notified_match_id = state.get("match_id")
    notified_goals = set(state.get("goals", []))
    notified_start = state.get("notified_start", False)
    notified_halftime = state.get("notified_halftime", False)
    notified_end = state.get("notified_end", False)

    while True:
        matches = get_matches()
        match = find_bayern_match(matches)

        if not match:
            print("No Bayern match this matchday. Checking again in 30 min.")
            time.sleep(1800)
            continue

        match_id = match["matchID"]

        # Reset state if it's a new match
        if match_id != notified_match_id:
            notified_match_id = match_id
            notified_goals = set()
            notified_start = False
            notified_halftime = False
            notified_end = False
            save_state({"match_id": match_id, "goals": [], "notified_start": False,
                        "notified_halftime": False, "notified_end": False})

        live = is_live(match)
        finished = match.get("matchIsFinished", False)

        # Notify match start
        if live and not notified_start:
            t1 = match["team1"]["teamName"]
            t2 = match["team2"]["teamName"]
            send(f"🔔 <b>KICK OFF!</b>\n{t1} vs {t2}\n🏟 Bundesliga")
            notified_start = True

        # Check goals
        if live or finished:
            fresh = get_match(match_id)
            if fresh:
                match = fresh

            goals = match.get("goals", [])
            for goal in goals:
                gid = str(goal.get("goalID", ""))
                if gid and gid not in notified_goals:
                    scorer = goal.get("goalGetterName", "Unknown")
                    minute = goal.get("matchMinute", "?")
                    is_own = goal.get("isOwnGoal", False)
                    is_penalty = goal.get("isPenalty", False)
                    s1, s2 = goal.get("scoreTeam1", 0), goal.get("scoreTeam2", 0)
                    t1 = match["team1"]["teamName"]
                    t2 = match["team2"]["teamName"]

                    tag = ""
                    if is_own:
                        tag = " (OG)"
                    elif is_penalty:
                        tag = " (P)"

                    # Determine if Bayern scored
                    emoji = "⚽" if not is_own else "😬"
                    send(f"{emoji} <b>GOAL!</b> {minute}'\n{scorer}{tag}\n{t1} {s1} - {s2} {t2}")
                    notified_goals.add(gid)

            # Halftime notification
            results = match.get("matchResults", [])
            halftime = next((r for r in results if r["resultTypeID"] == 1), None)
            if halftime and not notified_halftime:
                s1, s2 = halftime["pointsTeam1"], halftime["pointsTeam2"]
                t1 = match["team1"]["teamName"]
                t2 = match["team2"]["teamName"]
                send(f"🔔 <b>HALF TIME</b>\n{t1} {s1} - {s2} {t2}")
                notified_halftime = True

            # Full time notification
            if finished and not notified_end:
                score = format_score(match)
                send(f"🏁 <b>FULL TIME</b>\n{score}")
                notified_end = True

        save_state({
            "match_id": notified_match_id,
            "goals": list(notified_goals),
            "notified_start": notified_start,
            "notified_halftime": notified_halftime,
            "notified_end": notified_end
        })

        # Poll every 60s during live match, every 10min otherwise
        interval = 60 if live else 600
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Live={live} Finished={finished} — sleeping {interval}s")
        time.sleep(interval)

if __name__ == "__main__":
    main()
