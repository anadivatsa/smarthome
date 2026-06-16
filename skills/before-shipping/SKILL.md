# Before Shipping

Run this checklist before declaring any feature or fix "done". All four steps. No skipping.

---

## 1. Disk space (if anything was installed)

```bash
df -h /
```

Run before installing. Run again after. If venv was touched:
```bash
du -sh /home/anadivatsa/smarthome/venv/
```

The GPU torch incident added 4.7 GB silently. Disk on the Pi is not unlimited.

---

## 2. All services still running

```bash
for svc in hub wiz-lamp tgvoice bt_presence bt_jbl voice wakeword; do
  echo "$svc: $(systemctl is-active $svc.service 2>/dev/null)"
done
```

Expected: hub, wiz-lamp, tgvoice, bt_presence, bt_jbl → active. voice, wakeword → inactive (expected). If anything that was active is now inactive, something broke.

Or just run `skills/diagnose/check_services.sh`.

---

## 3. Test the actual user-facing flow end-to-end

"The code runs without error" is not done. Done means the user can trigger the feature through its real interface and it works.

- Scene changes: hit `GET /scene/<name>` and confirm lamp + TV respond.
- Telegram commands: send an actual message to the bot and verify the reply + action.
- NFC tags: if touching hub routing, scan the tag (or POST `/nfc/scan`) and confirm the scene fires.
- Presence: if touching bt_presence, check journalctl for the detection event.

---

## 4. If venv was touched

```bash
du -sh /home/anadivatsa/smarthome/venv/
```

Compare to baseline (~500 MB–1 GB range is normal; anything over 2 GB needs justification). If it grew unexpectedly, check what pulled in the bloat before committing.
