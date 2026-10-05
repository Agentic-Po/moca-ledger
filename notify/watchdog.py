#!/usr/bin/env python3
"""Report this project's own source freshness and crawl lag."""
import json, os, pathlib, sys, time, urllib.request, datetime as dt

ROOT  = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "alerts" / "state.json"
DEDUP_H = 6

sys.path.insert(1, str(ROOT))
try:                              # public Actions logs: no detector status
    from privlog import private_print
except ImportError:
    sys.path.insert(1, str(ROOT))
    from notify.privlog import private_print

# The two degradation thresholds, named because notify/commands.py reads them too.
# /status must never disclose a blind spot this watchdog has not already announced
# to the same group, so there is one definition and the two cannot drift apart
# (council §7 leaves gating read commands to the operator; this adds no new read surface).
MINDSET_STALE_H = 48
LAG_BLOCKS_MAX  = 900

def _may_send():
    """Only hosted runs or an explicit --force may send deployment warnings."""
    return bool(os.environ.get("GITHUB_ACTIONS") or "--force" in sys.argv)


def send(text):
    sys.path.insert(0, str(ROOT))
    from notify.telegram import send as tg
    if not _may_send():
        private_print(f"watchdog: NOT sending from a local run (use --force): {text[:60]!r}")
        return {"ok": True, "local": True}
    return tg(text)

def main():
    s = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = time.time(); last = s.get("watchdog", {}); alerts = []
    try:
        # run health is public (heartbeat.json); the mind-set age is private
        # (state["detector_health"], see notify/health.py)
        try:
            from health import load as _health
        except ImportError:
            sys.path.insert(1, str(ROOT))
            from notify.health import load as _health
        hb = _health(s, root=ROOT)
        from notify.health import mindset_warning
        warning = mindset_warning(hb)
        if warning:
            alerts.append(("mindset_stale", "⏳ <b>Address coverage incomplete</b>\n" + warning))
        if (hb.get("lag_blocks") or 0) > LAG_BLOCKS_MAX:
            alerts.append(("lag", f"⏳ <b>ledger behind tip</b>\n{hb.get('lag_blocks')} blocks — crawler running but not keeping up"))
    except Exception:
        pass
    if not any(key == "mindset_stale" for key, _ in alerts):
        last.pop("mindset_stale", None)
        last.pop("mindset_snapshot", None)
    fired, lost = 0, 0
    for key, msg in alerts:
        hours = 24 if key == "mindset_stale" else DEDUP_H
        snapshot = str(hb.get("mindset_generated_at") or hb.get("mindset_source")) if key == "mindset_stale" else None
        unchanged = key != "mindset_stale" or last.get("mindset_snapshot") == snapshot
        if unchanged and now - float(last.get(key, 0)) < hours * 3600: continue
        # Only a DELIVERED alert starts the reminder cooldown. Recording the attempt
        # meant an undelivered "ledger behind tip" was announced to nobody and then
        # suppressed through the cooldown on a green run.
        r = send(msg)
        if r.get("ok") and (r.get("result") or {}).get("message_id"):
            last[key] = now; fired += 1
            if key == "mindset_stale":
                last["mindset_snapshot"] = snapshot
            from notify.telegram import _log_out
            _log_out(r, "health")
        else:
            lost += 1
            private_print(f"watchdog: {key} NOT delivered ({r.get('error')}) — not deduped, "
                          f"it will be tried again next run", file=sys.stderr)
    s["watchdog"] = last
    STATE.write_text(json.dumps(s, indent=1))
    # Which conditions hold (address set stale, ledger behind) is detector status:
    # it goes to the group, never to the public Actions log.
    private_print(f"watchdog: {len(alerts)} condition(s), {fired} sent" + (f", {lost} undelivered" if lost else ""),
                  public="watchdog: ok" if not lost else "watchdog: an alert was NOT delivered")
    return 3 if lost else 0

if __name__ == "__main__": sys.exit(main())
