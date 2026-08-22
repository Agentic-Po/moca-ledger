#!/usr/bin/env python3
"""Cross-repo watchdog: this repo watches the dashboard, the dashboard watches this
repo, and healthchecks.io watches from outside GitHub. Any single dark layer is
detected by another; all three dark is what the external dead-man catches.

Run inside the crawl workflow (cheap: two unauthenticated public API calls).
"""
import json, os, pathlib, sys, time, urllib.request, datetime as dt

ROOT  = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "alerts" / "state.json"
PEER  = "Agentic-Po/skill-payout-dashboard"
DEDUP_H = 6

def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "moca-watchdog"}), timeout=25))

def send(text):
    sys.path.insert(0, str(ROOT))
    from notify.telegram import send as tg
    return tg(text)

def main():
    s = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = time.time(); last = s.get("watchdog", {}); alerts = []
    try:
        c = get(f"https://api.github.com/repos/{PEER}/commits/main")
        when = c["commit"]["committer"]["date"]
        age_min = (dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(when.replace("Z", "+00:00"))).total_seconds() / 60
        if age_min > 150:
            alerts.append(("peer_dark", f"⏳ <b>peer repo dark</b>\nlast commit {age_min:.0f} min ago (expected hourly)"))
    except Exception as e:
        alerts.append(("peer_unreachable", f"⏳ <b>peer repo unreachable</b>\n{str(e)[:90]}"))
    try:
        hb = json.loads((ROOT / "heartbeat.json").read_text())
        if (hb.get("mindset_age_h") or 0) > 48:
            alerts.append(("mindset_stale", f"⏳ <b>address set stale</b>\n{hb.get('mindset_age_h')} h old — detectors fell back to {hb.get('mindset_source')}"))
        if (hb.get("lag_blocks") or 0) > 900:
            alerts.append(("lag", f"⏳ <b>ledger behind tip</b>\n{hb.get('lag_blocks')} blocks — crawler running but not keeping up"))
    except Exception:
        pass
    fired = 0
    for key, msg in alerts:
        if now - float(last.get(key, 0)) < DEDUP_H * 3600: continue
        send(msg); last[key] = now; fired += 1
    s["watchdog"] = last
    STATE.write_text(json.dumps(s, indent=1))
    print(f"watchdog: {len(alerts)} condition(s), {fired} sent")
    return 0

if __name__ == "__main__": sys.exit(main())
