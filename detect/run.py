#!/usr/bin/env python3
"""Detection-floor orchestrator.

Per iteration: load ledger (data/*.jsonl + archived .gz) -> build ctx (salted-hash
mind set UNION chain, chain-only fallback) -> run every signal on rolling 10-min
windows -> reduce fires to episodes -> diff against alerts/state.json -> write
new/escalated findings (pending_send: true; notify/telegram.py sends them) ->
write incidents/<date>/<hhmm>-<signal>-<key8>/{finding.json,evidence.csv,view.png}
-> update heartbeat.json.

Flags: --dry-run (no writes), --quiet (counts only on stdout — public Actions
logs are world-readable), --as-of <block> (replay parity), --loop-if-hot.
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signals as S
from signals import Ctx, evaluate, episodes, summary, utc, SLOT, DAY

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "alerts", "state.json")
HEARTBEAT = os.path.join(ROOT, "heartbeat.json")
INCIDENTS = os.path.join(ROOT, "incidents")
TIER_RANK = {"digest": 0, "notify": 1, "page": 2}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="detection floor orchestrator")
    p.add_argument("--dry-run", action="store_true", help="evaluate only; write nothing")
    p.add_argument("--quiet", action="store_true", help="counts only on stdout")
    p.add_argument("--as-of", type=int, default=None, metavar="BLOCK", help="ignore rows after this block")
    p.add_argument("--loop-if-hot", action="store_true")
    p.add_argument("--no-balance", action="store_true",
                   help="skip the exit-leg balance watch (it is also skipped on --dry-run, "
                        "--as-of, or SKIP_BALANCE_WATCH=1 — replay/CI never make network calls)")
    p.add_argument("--max-loop-min", type=int, default=8)
    p.add_argument("--data", default=os.path.join(ROOT, "data"))
    p.add_argument("--parity-json", default=None, metavar="PATH",
                   help="write the per-signal summary (first fire, counts) as JSON — used by the CI parity gate")
    return p.parse_args(argv)


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"open": {}, "sent": {}, "telegram_offset": 0, "version": 1}


def current_findings(ctx):
    """Reduce all fires to one candidate finding per (signal, entity): the episode
    containing the strongest/latest evidence. Returns {finding_id: Finding}."""
    out = {}
    for sid, fires in ctx.fires.items():
        for ent, first, last, n, max_tier in episodes(fires):
            f = last                      # most recent state of the episode
            best = max((x for x in fires if x.key == ent and first.ts <= x.ts <= last.ts),
                       key=lambda x: (TIER_RANK.get(x.tier, 0), x.ts))
            f = best if TIER_RANK.get(best.tier, 0) > TIER_RANK.get(f.tier, 0) else f
            f.tier = max_tier
            prev = out.get(f.id)
            if prev is None or f.ts >= prev.ts:
                d = f.to_state()
                d["episode_first"] = utc(first.ts)
                d["episode_last"] = utc(last.ts)
                d["episode_fires"] = n
                out[f.id] = f
                f._state = d
    return out


def diff_state(state, findings, ctx, fresh_h=24):
    """New findings or escalations -> state['open'] with pending_send. Findings whose
    episode ended more than fresh_h ago are recorded as backfill (never sent)."""
    new, escalated = [], []
    for fid, f in findings.items():
        d = f._state
        d["mindset_source"] = ctx.mindset_source
        d["unit_source"] = ctx.unit_source.get(S.day_str(f.ts), "frozen")
        d["type_verified"] = False
        d["owner"] = ctx.thr.get("escalation_owner") or d.get("owner") or "UNASSIGNED"
        stale = (ctx.t1 - f.ts) > fresh_h * 3600
        cur = state["open"].get(fid)
        if cur is None:
            d["pending_send"] = not stale
            d["backfill"] = stale
            state["open"][fid] = d
            if not stale:
                new.append(f)
        else:
            was = TIER_RANK.get(cur.get("tier"), 0)
            now = TIER_RANK.get(d["tier"], 0)
            esc = now > was or (d.get("escalation") and d["escalation"] != cur.get("escalation"))
            keep = {k: cur[k] for k in ("pending_send", "backfill", "ack_role", "ack_by", "ack_ts", "ack_note", "snooze_until", "last_sent", "send_ok", "view_png") if k in cur}
            cur.update(d)
            cur.update(keep)
            if esc and not stale:
                cur["pending_send"] = True
                cur["escalated"] = True
                escalated.append(f)
            state["open"][fid] = cur
    return new, escalated


def write_incident(f, ctx):
    day = dt.datetime.fromtimestamp(f.ts, dt.UTC)
    key8 = f.key.replace("0x", "")[:8] if f.key else f.id[:8]
    sig = f.signal.replace("#", "").replace("/", "-").lower()
    folder = os.path.join(INCIDENTS, day.strftime("%Y-%m-%d"), f"{day.strftime('%H%M')}-{sig}-{key8}")
    os.makedirs(folder, exist_ok=True)
    png = os.path.join(folder, "view.png")
    import views
    ok = views.render(f, ctx, png)
    f.view_png = os.path.relpath(png, ROOT) if ok else None
    doc = f.to_state()
    doc["view_png"] = f.view_png
    with open(os.path.join(folder, "finding.json"), "w") as fh:
        json.dump(doc, fh, indent=1, default=str)
    with open(os.path.join(folder, "evidence.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        for row in (f.evidence or []):
            w.writerow(row if isinstance(row, (list, tuple)) else [row])
    return folder, f.view_png


def heartbeat(ctx, state, ok=True):
    hb = {}
    if os.path.exists(HEARTBEAT):
        try:
            hb = json.load(open(HEARTBEAT))
        except Exception:
            hb = {}
    now = dt.datetime.now(dt.UTC)
    open_f = [v for v in state["open"].values() if not (v.get("ack_role") or v.get("ack_by"))]
    per_day = {}
    for sid, fires in ctx.fires.items():
        per_day[sid] = sum(1 for f in fires if f.ts >= ctx.t1 - DAY)
    hb.update({
        "run_ts": now.isoformat(timespec="seconds"),
        "detect_ok": ok,
        "rows_total": len(ctx.rows),
        "ledger_last": utc(ctx.t1),
        "lag_blocks": int((now.timestamp() - ctx.t1) / 2),
        "mindset_age_h": ctx.mindset_age_h,
        "mindset_source": ctx.mindset_source,
        "open_findings": {
            "page": sum(1 for v in open_f if v.get("tier") == "page"),
            "notify": sum(1 for v in open_f if v.get("tier") == "notify"),
            "digest": sum(1 for v in open_f if v.get("tier") == "digest"),
        },
        "fires_last_24h_total": sum(per_day.values()) if isinstance(per_day, dict) else per_day,
        "outflow_x_now": getattr(ctx, "outflow_x", {}).get(ctx.s1),
    })
    with open(HEARTBEAT, "w") as fh:
        json.dump(hb, fh, indent=1)


def write_public_state(state, ctx):
    """Publishable projection of the findings: hashed key, tier, first seen.

    The plaintext state names which wallets we flagged, at what value against which
    threshold, plus the recommended action — a calibration oracle for the operator
    it is watching. It is gitignored; this hashed projection is what ships."""
    import hashlib
    salt = os.environ.get("MINDSET_SALT", "")
    h = lambda x: hashlib.sha256((salt + str(x).lower()).encode()).hexdigest()[:16]
    pub = {"generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "note": "hashed projection; entity keys are salted hashes, thresholds and actions are private",
           "hash": "sha256(salt+lower(key))[:16]",
           "findings": sorted(
               ({"h": h(v.get("key")), "signal": v.get("signal"), "tier": v.get("tier"),
                 "first_seen": v.get("first_ts"), "acked": bool(v.get("ack_role") or v.get("ack_by"))}
                for v in state.get("open", {}).values()),
               key=lambda r: str(r.get("first_seen")))}
    with open(os.path.join(ROOT, "alerts", "state-public.json"), "w") as fh:
        json.dump(pub, fh, indent=1)


def one_pass(a):
    ctx = Ctx(root=ROOT, as_of_block=a.as_of, data_dir=a.data)
    evaluate(ctx)
    if a.parity_json:
        with open(a.parity_json, "w") as fh:
            json.dump(summary(ctx), fh, indent=1)
    findings = current_findings(ctx)
    # ---- exit-leg balance watch (build plan 1.7): live slots only, never in
    # replay/CI (--dry-run, --as-of, --no-balance and SKIP_BALANCE_WATCH=1 all skip it)
    if not (a.dry_run or a.no_balance or a.as_of is not None
            or os.environ.get("SKIP_BALANCE_WATCH")):
        try:
            import balance_watch
            for f in balance_watch.poll(root=ROOT, thresholds=ctx.thr, quiet=a.quiet):
                ctx.fires.setdefault(f.signal, []).append(f)
                findings[f.id] = f
        except Exception as e:  # fail-soft: the ledger signals must still land
            print(f"balance_watch: soft-fail ({type(e).__name__})")
    state = load_state()
    new, escalated = diff_state(state, findings, ctx)
    n_inc = 0
    if not a.dry_run:
        for f in new + escalated:
            folder, png = write_incident(f, ctx)
            state["open"][f.id]["view_png"] = png
            n_inc += 1
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as fh:
            json.dump(state, fh, indent=1)
        write_public_state(state, ctx)
        heartbeat(ctx, state)
    # ---- stdout policy: quiet = counts only, never entities/actions
    tiers = lambda lst: {t: sum(1 for f in lst if f.tier == t) for t in ("page", "notify", "digest") if any(f.tier == t for f in lst)}
    if a.quiet:
        print(f"detect: rows={len(ctx.rows)} new={len(new)} escalated={len(escalated)} "
              f"new_by_tier={tiers(new)} incidents={n_inc} mindset={ctx.mindset_source}")
    else:
        print(f"detect: {len(ctx.rows)} rows to {utc(ctx.t1)} UTC · mindset {ctx.mindset_source} "
              f"(age {ctx.mindset_age_h} h)")
        recent = [(sid, f) for sid, fires in sorted(ctx.fires.items()) for f in fires if f.ts >= ctx.t1 - DAY]
        by = {}
        for sid, f in recent:
            by.setdefault((sid, f.key, f.tier), []).append(f)
        print(f"fires in last 24 h: {len(recent)} across {len(by)} (signal, entity) pairs")
        for (sid, key, tier), lst in sorted(by.items(), key=lambda x: -TIER_RANK.get(x[0][2], 0)):
            print(f"  {tier:6s} {sid:5s} {key[:10]:12s} x{len(lst)}  last {utc(lst[-1].ts)}  {lst[-1].detail[:60]}")
        print(f"state: {len(new)} new, {len(escalated)} escalated, "
              f"{sum(1 for v in state['open'].values() if v.get('pending_send'))} pending send")
    hot = any(v.get("tier") in ("page", "notify") and not (v.get("ack_role") or v.get("ack_by")) and v.get("pending_send")
              for v in state["open"].values())
    return hot


def main():
    a = parse_args()
    t_end = time.time() + a.max_loop_min * 60
    hot = one_pass(a)
    while a.loop_if_hot and hot and not a.dry_run and time.time() + 120 < t_end:
        time.sleep(120)
        hot = one_pass(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
