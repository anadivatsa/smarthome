#!/usr/bin/env bash
# test_auth.sh — Neo hub authentication smoke tests
# Usage: bash test_auth.sh

set -euo pipefail

KEY=$(grep NEO_API_KEY "$(dirname "$0")/hub.env" | cut -d= -f2)
LAN_BASE="http://192.168.1.8:5001"
TS_IP=$(tailscale ip -4 2>/dev/null || echo "")
TS_BASE="http://${TS_IP}:5001"

GREEN='\033[0;32m'
RED='\033[0;31m'
RESET='\033[0m'
PASS="${GREEN}PASS${RESET}"
FAIL="${RED}FAIL${RESET}"

FAILS=0

check() {
    local desc="$1"
    local url="$2"
    local expected_code="$3"

    actual=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null || echo "000")

    if [[ "$actual" == "$expected_code" ]]; then
        echo -e "  ${PASS}  [${actual}] ${desc}"
    else
        echo -e "  ${FAIL}  [${actual} != ${expected_code}] ${desc}"
        FAILS=$((FAILS + 1))
    fi
}

check_json() {
    local desc="$1"
    local url="$2"

    body=$(curl -s --max-time 5 "$url" 2>/dev/null || echo "")
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null || echo "000")

    if [[ "$code" == "200" ]] && echo "$body" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null; then
        echo -e "  ${PASS}  [${code} + valid JSON] ${desc}"
    else
        echo -e "  ${FAIL}  [${code}] ${desc}"
        FAILS=$((FAILS + 1))
    fi
}

echo ""
echo "═══════════════════════════════════════════"
echo "  Neo Hub — Auth Smoke Tests"
echo "  LAN: ${LAN_BASE}"
echo "  Tailscale: ${TS_BASE}"
echo "═══════════════════════════════════════════"
echo ""

echo "── LAN ──────────────────────────────────────"
check "1. LAN WITH key → 200"       "${LAN_BASE}/scenes?key=${KEY}"   "200"
check "2. LAN WITHOUT key → 401"    "${LAN_BASE}/scenes"               "401"

echo ""
echo "── Tailscale ────────────────────────────────"
if [[ -z "$TS_IP" ]]; then
    echo -e "  ${FAIL}  Tailscale IP not available — skipping tests 3 & 4"
    FAILS=$((FAILS + 2))
else
    check "3. Tailscale WITH key → 200"    "${TS_BASE}/scenes?key=${KEY}"   "200"
    check "4. Tailscale WITHOUT key → 401" "${TS_BASE}/scenes"               "401"
fi

echo ""
echo "── Public endpoints ─────────────────────────"
# /spotify/auth returns 503 when Spotify not configured, but NOT 401 — either is acceptable
sp_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${LAN_BASE}/spotify/auth" 2>/dev/null || echo "000")
if [[ "$sp_code" != "401" ]]; then
    echo -e "  ${PASS}  [${sp_code}] 5. /spotify/auth excluded from auth (not 401)"
else
    echo -e "  ${FAIL}  [${sp_code}] 5. /spotify/auth should not return 401"
    FAILS=$((FAILS + 1))
fi

echo ""
echo "── /api/info ────────────────────────────────"
check_json "6. /api/info WITH key → 200 + valid JSON" "${LAN_BASE}/api/info?key=${KEY}"

echo ""
echo "═══════════════════════════════════════════"
if [[ "$FAILS" -eq 0 ]]; then
    echo -e "  ${GREEN}All tests passed.${RESET}"
else
    echo -e "  ${RED}${FAILS} test(s) failed.${RESET}"
fi
echo "═══════════════════════════════════════════"
echo ""

exit "$FAILS"
