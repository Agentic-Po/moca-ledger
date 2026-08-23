#!/usr/bin/env python3
"""Weekly dead-man + schedule keep-alive.

GitHub disables schedules on public repos after 60 days without repository activity,
so this both stamps activity and reports the observed run gap: silence on a Monday
means both layers are broken.
"""
import json, os, pathlib, sys, urllib.request, datetime as dt

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

def main():
    hb = {}
    try: hb = json.loads((ROOT / "heartbeat.json").read_text())
    except Exception: pass
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
    (ROOT / "alerts" / "weekly.json").write_text(json.dumps(stamp, indent=1))
    from notify.telegram import send, _log_out
    _log_out(send(f"🗓 <b>weekly check</b>\n"
         f"detector alive · last run {hb.get('run_ts','?')}\n"
         f"observed run gap: p50 {p50} min · p95 {p95} min (n={len(gaps)})\n"
         f"open findings: {hb.get('open_findings')}\n"
         f"<i>set the healthchecks period from p95; silence on a Monday means both layers are down</i>", silent=True), "health")
    print(json.dumps(stamp))
    return 0

if __name__ == "__main__": sys.exit(main())
