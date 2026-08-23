#!/usr/bin/env python3
"""CI gate for the detection floor (runs on the committed ledger).

G3 first-fire deadlines (ledger/block time, slot-start convention):
  - 4b  fires <= 2026-08-19 08:00 UTC   (satisfied by its recon fire)
  - 10  first page-tier fire <= 2026-08-19 13:20 UTC  (n>=50 gate)
  - 11  fires <= 2026-08-20 16:10 UTC
  - 15  fires <= 2026-08-21 01:30 UTC
G4 zero page-tier fires in the benign windows 2026-07-16 and 2026-07-22, and
   zero page/notify fires keyed on an excluded (allow-listed) creator, ever.
G5 no single class in labels-lite.json, and no baseline window length, may move
   S-A's threshold by more than 2x. The file that classes entities is read by
   exactly ONE line of detector code, so a wrong class is a silent switch.
G2 parity: `run.py --as-of <block>` == `replay.py --as-of <block>` per signal.

Stdout: pass/fail lines and counts only — never entity keys.
Exit 0 = green.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "detect"))

from signals import Ctx, evaluate, shadow_signals, shadow_tier, ts_of, utc, day_str, _pct  # noqa: E402
from signals.composite import MEMBERS as COMPOSITE_MEMBERS  # noqa: E402
from signals import slow_harvest  # noqa: E402  (G5 re-runs this one signal on its own)

FAILS = []


def check(name, ok, note=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({note})" if note else ""))
    if not ok:
        FAILS.append(name)


def main():
    print("gate: evaluating committed ledger ...")
    ctx = Ctx(root=ROOT)
    if not ctx.salt:
        print("gate: FAIL — no MINDSET_SALT available; excluded-creator logic untestable")
        return 1
    evaluate(ctx)

    def first(sid, tier=None):
        fires = ctx.fires.get(sid, [])
        if tier:
            fires = [f for f in fires if f.tier == tier]
        return min((f.ts for f in fires), default=None)

    # ---- G3 deadlines
    deadlines = [
        ("4b any-fire",   first("4b"),           "2026-08-19T08:00"),
        ("10 page n>=50", first("10", "page"),   "2026-08-19T13:20"),
        ("11 page",       first("11"),           "2026-08-20T16:10"),
        ("15 page",       first("15"),           "2026-08-21T01:30"),
    ]
    for name, ts, dl in deadlines:
        ok = ts is not None and ts <= ts_of(dl)
        check(f"G3 {name} <= {dl}Z", ok, f"first fire {utc(ts) if ts else 'never'}")

    # ---- G4 benign windows: zero page-tier fires anywhere in them
    windows = [(ts_of("2026-07-16T00:00"), ts_of("2026-07-17T00:00")),
               (ts_of("2026-07-22T00:00"), ts_of("2026-07-23T00:00"))]
    bad = 0
    for sid, fires in ctx.fires.items():
        for f in fires:
            if f.tier == "page" and any(a <= f.ts < b for a, b in windows):
                bad += 1
    check("G4 zero page fires on 2026-07-16 / 2026-07-22", bad == 0, f"{bad} page fires in benign windows")

    # ---- G4b excluded creators never fire page/notify
    bad2 = 0
    for sid, fires in ctx.fires.items():
        for f in fires:
            if f.tier in ("page", "notify") and f.key.startswith("0x") and ctx.is_internal(f.key):
                bad2 += 1
    check("G4 zero page/notify fires on excluded creators", bad2 == 0, f"{bad2} fires")

    # ---- G6 #14 is a FIELD, not an alert (council §5, vote 5). Nothing emits it,
    # so notify/explain.py no longer carries copy for it. If that ever changes, the
    # channel would render a signal through the terse fallback — which announces
    # itself as a failure — for an alert whose own recommended action was "Nothing."
    n14 = len(ctx.fires.get("14", []))
    check("G6 #14 emits no findings", n14 == 0, f"{n14} fires")
    xs = sorted(ctx.outflow_x.values())
    check("G6 #14 still computes the multiple run.py stamps on the heartbeat",
          len(xs) > 1000, f"{len(xs)} slot(s)")
    # `med > 0` alone let a ~1e-18 denominator through and produced 8.4e17.
    check("G6 the outflow multiple is a number a person could read",
          xs and xs[-1] < 100000, f"max {xs[-1]:,.1f}" if xs else "none")

    # ---- G7 no signal may learn its bar from the attack. Replay cannot catch this:
    # replay evaluates the HISTORIC per-slot threshold, and the damage is to the FORWARD
    # one — the bar in force tomorrow, computed from a window that now contains the
    # incident. Measured before the cap: EV's page bar was 615 equip payouts a day
    # against a busiest-organic-day-ever of 124, because a "p95" over 28 points is the
    # second-largest value and three of those points were 205 / 4,247 / 17,015.
    import statistics as _st
    _daily = {}
    for _ts, _t, _v, _bd, _tx in ctx.equips:
        if not ctx.is_internal(_t):
            _daily[day_str(_ts)] = _daily.get(day_str(_ts), 0) + 1
    _days = sorted(_daily)
    _win = sorted(_daily[d] for d in _days[-28:])
    _organic_max = max((_daily[d] for d in _days if d < "2026-08-19"), default=0)
    _base = min(_pct(_win, 0.95), ctx.thr["ev_baseline_cap_mult"] * _st.median(_win))
    _p95 = max(ctx.thr["ev_p95_default"], _base)
    _bar = int(ctx.thr["ev_page_mult"] * _p95)
    check("G7 EV's FORWARD page bar is not loosened by the incident it is meant to catch",
          _bar <= 3 * _organic_max,
          f"bar {_bar} vs {_organic_max} on the busiest organic day ({_bar/max(_organic_max,1):.1f}x)")

    # ---- G5 nothing outside the data may set S-A's bar.
    # labels-lite.json is read by exactly one line of detector code — the organic
    # filter in slow_harvest — so while the baseline was a max-of-maxima, ONE class
    # decided the threshold, and the wallet holding that day is by construction the
    # most harvest-like creator nobody has classed yet. An uncaught farm raised the
    # bar for catching farms.
    #
    # sa_floor hides this at today's volumes: with the floor left in, this check also
    # passes on the max-of-maxima and proves nothing. So the floor is dropped to 1 to
    # compare the baselines themselves. Measured: 348x before the percentile, 1.0x
    # after. Bounded rather than asserted equal, because a strict form would go red on
    # a healthy system the moment the organic population grows.
    def sa_max_thr(lite=None, days=None):
        keep = (ctx.lite, ctx.thr["sa_floor"], ctx.thr["sa_young_floor"], ctx.thr["sa_baseline_days"])
        if lite is not None:
            ctx.lite = lite
        ctx.thr["sa_floor"] = ctx.thr["sa_young_floor"] = 1
        if days:
            ctx.thr["sa_baseline_days"] = days
        try:
            return max((f.threshold for f in slow_harvest.run(ctx)), default=0)
        finally:
            (ctx.lite, ctx.thr["sa_floor"], ctx.thr["sa_young_floor"],
             ctx.thr["sa_baseline_days"]) = keep

    lab, unlab = sa_max_thr(), sa_max_thr(lite={})
    check("G5 no class moves S-A's baseline by more than 2x", unlab <= 2 * lab,
          f"bar {lab} classed vs {unlab} unclassed")
    # 1.25x, not 2x: at 2x this check passes on the max-of-maxima too (11 -> 20 is
    # 1.8x) and would prove nothing. A percentile only moves with the population, and
    # a LONGER window reaches back to quieter days, so the bar should fall or hold as
    # the window grows — never climb. The max climbs by construction.
    d1 = ctx.thr["sa_baseline_days"]
    short, long = sa_max_thr(days=d1), sa_max_thr(days=2 * d1)
    check("G5 doubling the baseline window does not raise the bar", long <= 1.25 * short,
          f"bar {short} at {d1} d vs {long} at {2 * d1} d")

    # ---- G5 shadow mode. INV-#10/#11 are the #10/#11 shapes on the invoke band and
    # are uncalibrated: every fire either of them produces on this ledger belongs to
    # ONE creator the private label file classes benign, and invoke rewards have been
    # paused since 21 Aug, so there is no traffic to re-measure on. They must reach
    # the channel as digest lines and never as pages.
    applied, _refused = shadow_signals(ctx.thr)
    check("G5 the committed defaults put INV-10 and INV-11 in shadow",
          {"INV-10", "INV-11"} <= applied, ",".join(sorted(applied)) or "none")
    check("G5 a measured pager cannot be put in shadow by an override",
          shadow_signals({"shadow_signals": ["10", "11", "15", "4b", "S-F", "S-X", "INV-10"]})
          == ({"INV-10"}, ["10", "11", "15", "4b", "S-F", "S-X"]),
          str(shadow_signals({"shadow_signals": ["10", "INV-10"]})))
    check("G5 a shadowed page is demoted to digest and remembers what it was",
          shadow_tier(ctx.thr, "INV-10", "page") == ("digest", "page"),
          str(shadow_tier(ctx.thr, "INV-10", "page")))
    check("G5 a signal that is not in shadow is left alone",
          shadow_tier(ctx.thr, "10", "page") == ("page", ""))
    check("G5 a shadowed signal cannot page through the composite rule",
          not (applied & COMPOSITE_MEMBERS), ",".join(sorted(applied & COMPOSITE_MEMBERS)))

    inv = ctx.fires.get("INV-10", []) + ctx.fires.get("INV-11", [])
    leaked = [f for f in inv if shadow_tier(ctx.thr, f.signal, f.tier)[0] != "digest"]
    check("G5 no invoke-band fire reaches the channel above digest", not leaked,
          f"{len(leaked)} of {len(inv)} would page")
    pre = [f for f in inv if f.ts < ts_of("2026-08-19T00:00")]
    check("G5 neither invoke-band signal fires in the 44 days before the incident",
          not pre, f"{len(pre)} fire(s) before 2026-08-19")
    check("G5 both invoke-band signals fired on the incident",
          bool(ctx.fires.get("INV-10")) and bool(ctx.fires.get("INV-11")),
          f"INV-10 {len(ctx.fires.get('INV-10', []))}, INV-11 {len(ctx.fires.get('INV-11', []))}")
    check("G5 an override cannot LIFT a signal out of the committed shadow list",
          {"INV-10", "INV-11"} <= shadow_signals(dict(ctx.thr, shadow_signals=["INV-10"]))[0],
          str(sorted(shadow_signals(dict(ctx.thr, shadow_signals=["INV-10"]))[0])))

    # ---- S-B cluster layer: creators that share a 2-hop sink
    sb = ctx.fires.get("S-B", [])
    sb_first = min((f.ts for f in sb), default=None)
    check("S-B names the shared-sink cluster by 2026-08-21 09:20Z",
          sb_first is not None and sb_first <= ts_of("2026-08-21T09:20"),
          f"first fire {utc(sb_first) if sb_first else 'never'}")
    sb_benign = [f for f in sb if any(a <= f.ts < b for a, b in windows)]
    check("S-B is silent in the benign windows", not sb_benign, f"{len(sb_benign)} fires")
    check("the S-B detector never emits above notify (run.py may still escalate a contained case)",
          all(f.tier == "notify" for f in sb),
          f"{sum(1 for f in sb if f.tier != 'notify')} non-notify fires")
    sb_members = {m for f in sb for kind, m, _ in f.evidence if kind == "member"}
    check("no excluded creator is ever inside an S-B cluster",
          not [m for m in sb_members if ctx.is_internal(m)], f"{len(sb_members)} members")
    sb_sinks = {x for f in sb for kind, x, _ in f.evidence if kind == "sink"}
    check("no allow-listed platform address is ever an S-B collecting wallet",
          not (sb_sinks & ctx.allow), f"{len(sb_sinks)} sinks")

    # The 24 h rule has no example in this ledger - the August operator never
    # spread out - so its branch is driven here rather than shipped unexercised.
    from signals.cluster import shared_sinks, cluster_map, cluster_id, fires_share, fires_aggregate
    edges = [(100, "0xc1", "0xh1"), (200, "0xh1", "0xsink"),
             (300, "0xc2", "0xh2"), (400, "0xh2", "0xsink"),
             (500, "0xc3", "0xh3"), (600, "0xh3", "0xown")]
    ss = shared_sinks(edges, ["0xc1", "0xc2", "0xc3"])
    check("two creators two hops from one address are one cluster",
          set(ss.get("0xsink", ())) == {"0xc1", "0xc2"}, str(sorted(ss)))
    root_of, sinks_of = cluster_map(ss)
    check("a creator with only its own sink is not clustered",
          "0xc3" not in root_of, str(sorted(root_of)))
    check("the cluster carries the shared address as its sink",
          sinks_of.get(root_of["0xc1"]) == ["0xsink"], str(sinks_of))
    backwards = [(100, "0xc1", "0xh1"), (50, "0xh1", "0xsink"), (100, "0xc2", "0xh1")]
    check("a hop that paid out before it was paid does not carry the link onward",
          "0xsink" not in shared_sinks(backwards, ["0xc1", "0xc2"]))
    check("the cluster id depends on the sink set, not on its order",
          cluster_id(["0xb", "0xa"]) == cluster_id(["0xa", "0xb"]))
    check("the 6 h rule holds when one member is over the line on its own",
          not fires_share(60, 100, 50, 50, 0.45), "member at 50% is #10's case")
    check("the 6 h rule fires when only the group is over the line",
          fires_share(60, 100, 40, 50, 0.45), "group 60%, largest 40%")
    check("the 6 h rule holds below the n gate", not fires_share(30, 49, 20, 50, 0.45), "n=49")
    check("the 24 h rule fires on a farm split under the per-creator floor",
          fires_aggregate(40, 14, 30.0, 15), "40 across the group, largest 14")
    check("the 24 h rule holds when a member reaches the per-creator floor",
          not fires_aggregate(40, 15, 30.0, 15), "largest 15 is S-A's case")
    check("no excluded creator wallet is ever an S-B collecting wallet",
          not [x for x in sb_sinks if ctx.is_internal(x)], f"{len(sb_sinks)} sinks")

    # ---- G5 the demotion must be applied by the PIPELINE, not merely be available as
    # a helper. Every check above is satisfied by the helper alone: deleting the one
    # line in run.py that calls shadow_tier() leaves them all green while INV-10 goes
    # back to tier "page" in alerts/state.json. Runs LAST in this block because
    # current_findings() rewrites Finding.tier on ctx.fires in place.
    import run as RUN  # noqa: E402  (detect/ is on sys.path from the header)
    reduced = list(RUN.current_findings(ctx).values())
    shadowed = [f for f in reduced if str(f.signal).lstrip("#") in applied]
    leaked2 = [f for f in shadowed if f.to_state().get("tier") != "digest"]
    check("G5 run.py demotes every shadowed finding before it is written to state",
          bool(shadowed) and not leaked2,
          f"{len(leaked2)} of {len(shadowed)} still above digest in state")
    check("G5 a demoted finding records in state the tier it would have had",
          all(f.to_state().get("shadow_of") == "page" for f in shadowed),
          str(sorted({f.to_state().get("shadow_of") for f in shadowed})))

    # ---- G2 parity: run.py --as-of == replay.py per signal
    as_of = ctx.as_of_block
    with tempfile.TemporaryDirectory() as td:
        a, b = os.path.join(td, "run.json"), os.path.join(td, "replay.json")
        r1 = subprocess.run([sys.executable, os.path.join(ROOT, "detect", "run.py"),
                             "--dry-run", "--quiet", "--as-of", str(as_of), "--parity-json", a],
                            capture_output=True, text=True, cwd=ROOT)
        r2 = subprocess.run([sys.executable, os.path.join(ROOT, "detect", "replay.py"),
                             "--as-of", str(as_of), "--json", b],
                            capture_output=True, text=True, cwd=ROOT)
        ok = r1.returncode == 0 and r2.returncode == 0
        if ok:
            ja, jb = json.load(open(a)), json.load(open(b))
            ok = ja == jb
            diff = [k for k in set(ja) | set(jb) if ja.get(k) != jb.get(k)]
            check("G2 run.py --as-of == replay.py per signal", ok, "differs: " + ",".join(diff) if diff else "")
        else:
            check("G2 run.py --as-of == replay.py per signal", False,
                  f"exit {r1.returncode}/{r2.returncode}")

    # ---- stdout hygiene of the quiet path (no 0x entities in logs)
    quiet_out = r1.stdout if r1 else ""
    check("quiet stdout carries no wallet keys", "0x" not in quiet_out)

    if FAILS:
        print(f"gate: FAIL — {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print("gate: OK — all checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
