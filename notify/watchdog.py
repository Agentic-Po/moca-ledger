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

# The two degradation thresholds, named because notify/commands.py reads them too.
# /status must never disclose a blind spot this watchdog has not already announced
# to the same group, so there is one definition and the two cannot drift apart
# (council §7 leaves gating read commands to Po; this adds no new read surface).
MINDSET_STALE_H = 48
LAG_BLOCKS_MAX  = 900

def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "moca-watchdog"}), timeout=25))

def _may_send():
    """Only a scheduled run may page this channel.

    Every condition this module checks is about the DEPLOYMENT — is the peer repo
    committing, is the crawler keeping up — and none of them is meaningful from a
    laptop. Running it by hand posted a false "peer repo unreachable" to the live
    security group, because an unauthenticated GitHub API call from a developer
    machine is rate-limited long before the peer repo is actually dark. A false
    alert in this channel costs more than a missed local test: it is the channel
    people are meant to trust at 04:00. --force is the deliberate override."""
    return bool(os.environ.get("GITHUB_ACTIONS") or "--force" in sys.argv)


def send(text):
    sys.path.insert(0, str(ROOT))
    from notify.telegram import send as tg
    if not _may_send():
        print(f"watchdog: NOT sending from a local run (use --force): {text[:60]!r}")
        return {"ok": True, "local": True}
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
        if (hb.get("mindset_age_h") or 0) > MINDSET_STALE_H:
            alerts.append(("mindset_stale", f"⏳ <b>address set stale</b>\n{hb.get('mindset_age_h')} h old — detectors fell back to {hb.get('mindset_source')}"))
        if (hb.get("lag_blocks") or 0) > LAG_BLOCKS_MAX:
            alerts.append(("lag", f"⏳ <b>ledger behind tip</b>\n{hb.get('lag_blocks')} blocks — crawler running but not keeping up"))
    except Exception:
        pass
    fired, lost = 0, 0
    for key, msg in alerts:
        if now - float(last.get(key, 0)) < DEDUP_H * 3600: continue
        # Only a DELIVERED alert starts the six-hour dedupe. Recording the attempt
        # meant an undelivered "ledger behind tip" was announced to nobody and then
        # suppressed for six hours, on a green run (fix-round critic #6).
        r = send(msg)
        if r.get("ok"):
            last[key] = now; fired += 1
        else:
            lost += 1
            print(f"watchdog: {key} NOT delivered ({r.get('error')}) — not deduped, "
                  f"it will be tried again next run", file=sys.stderr)
    s["watchdog"] = last
    STATE.write_text(json.dumps(s, indent=1))
    print(f"watchdog: {len(alerts)} condition(s), {fired} sent" + (f", {lost} undelivered" if lost else ""))
    return 3 if lost else 0

if __name__ == "__main__": sys.exit(main())
