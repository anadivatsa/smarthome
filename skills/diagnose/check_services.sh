#!/usr/bin/env bash
# One-shot diagnostic for Neo smart home services.

SERVICES=(hub wiz-lamp tgvoice bt_presence bt_jbl voice wakeword)
EXPECTED_INACTIVE=(voice wakeword)

echo "==============================="
echo " NEO SERVICES"
echo "==============================="
for svc in "${SERVICES[@]}"; do
    status=$(systemctl is-active "${svc}.service" 2>/dev/null; true)
    printf "  %-20s %s\n" "${svc}" "${status}"
done

echo ""
echo "==============================="
echo " DISK"
echo "==============================="
df -h / | tail -1 | awk '{printf "  Used: %s / %s (%s)\n", $3, $2, $5}'

echo ""
echo "==============================="
echo " RAM"
echo "==============================="
free -h | awk '/^Mem:/ {printf "  Used: %s / %s\n", $3, $2}'

echo ""
echo "==============================="
echo " CPU TEMP"
echo "==============================="
if command -v vcgencmd &>/dev/null; then
    printf "  %s\n" "$(vcgencmd measure_temp)"
else
    printf "  %s\n" "$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null | awk '{printf "temp=%.1f'"'"'C", $1/1000}' || echo 'unavailable')"
fi

echo ""

# For any service that is not active (and not expected inactive), show last 20 log lines
for svc in "${SERVICES[@]}"; do
    status=$(systemctl is-active "${svc}.service" 2>/dev/null; true)
    if [[ "$status" != "active" ]]; then
        expected=false
        for ei in "${EXPECTED_INACTIVE[@]}"; do
            [[ "$svc" == "$ei" ]] && expected=true && break
        done
        if [[ "$expected" == "false" ]]; then
            echo "==============================="
            echo " LOGS: ${svc} (${status})"
            echo "==============================="
            journalctl -u "${svc}.service" -n 20 --no-pager -q 2>/dev/null || echo "  (no logs)"
            echo ""
        fi
    fi
done
