#!/usr/bin/env python3
"""Weekly dead-man + schedule keep-alive.

GitHub disables schedules on public repos after 60 days without repository activity,
so this stamps activity after confirmed delivery and reports the observed run gap.
"""
import json, os, pathlib, sys, urllib.request, datetime as dt

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

def main():
    # Public run health + the private detector health (open findings live only in
    # the private state; weekly.yml restores it before this runs).
    from notify.health import load as _health
    from notify.privlog import private_print
    hb = _health()
    # observed cadence from the last commits the crawl wrote
    gaps = []
    try:
        url = "https://api.github.com/repos/Agentic-Po/moca-ledger/commits?per_page=60"
        cs = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "weekly"}), timeout=25))
        ts = [dt.datetime.fromisoformat(c["commit"]["committer"]["date"].replace("Z", "+00:00"))
              for c in cs if c["commit"]["message"].startswith("crawl ")]
        gaps = sorted(round((ts[i] - ts[i + 1]).total_seconds() / 60) for i in range(len(ts) - 1))
    except Exception:
        pass
    # A percentile needs a population. With samples: 7 this published p50 13 / p95 56,
    # where "p95" is gaps[int(7*0.95)] = the largest of seven — and that number was then
    # quoted at people in the channel as what the detector can deliver. Below MIN_SAMPLES
    # it reports None and says how many it had, rather than dressing a maximum as a p95.
    MIN_SAMPLES = 20
    p50 = gaps[len(gaps) // 2] if gaps else None
    p95 = (gaps[min(len(gaps) - 1, int(0.95 * (len(gaps) - 1)))]
           if len(gaps) >= MIN_SAMPLES else None)
    stamp = {"stamped_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "run_gap_min_p50": p50, "run_gap_min_p95": p95, "samples": len(gaps)}
    from notify.telegram import send, _log_out
    response = send(f"🗓 <b>weekly check</b>\n"
         f"latest recorded run {hb.get('run_ts','?')}\n"
         f"observed run gap: p50 {p50} min · p95 {p95} min (n={len(gaps)})\n"
         f"open findings: {hb.get('open_findings')}\n"
         f"<i>These are observed gaps between published crawl updates, not a delivery guarantee. "
         f"Schedules can be delayed; quiet is not an all-clear. Check workflow and source health if updates stop.</i>", silent=True)
    if not response.get("ok") or not (response.get("result") or {}).get("message_id"):
        private_print("weekly: delivery not confirmed", public="weekly: delivery not confirmed", file=sys.stderr)
        return 3
    _log_out(response, "health")
    (ROOT / "alerts" / "weekly.json").write_text(json.dumps(stamp, indent=1))
    private_print(json.dumps(stamp), public="weekly: stamped")
    return 0

if __name__ == "__main__": sys.exit(main())
