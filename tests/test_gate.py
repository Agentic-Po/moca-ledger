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

from signals import Ctx, evaluate, ts_of, utc  # noqa: E402
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
