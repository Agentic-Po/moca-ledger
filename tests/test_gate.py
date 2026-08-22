#!/usr/bin/env python3
"""CI gate for the detection floor (runs on the committed ledger).

G3 first-fire deadlines (ledger/block time, slot-start convention):
  - 4b  fires <= 2026-08-19 08:00 UTC   (satisfied by its recon fire)
  - 10  first page-tier fire <= 2026-08-19 13:20 UTC  (n>=50 gate)
  - 11  fires <= 2026-08-20 16:10 UTC
  - 15  fires <= 2026-08-21 01:30 UTC
G4 zero page-tier fires in the benign windows 2026-07-16 and 2026-07-22, and
   zero page/notify fires keyed on an excluded (allow-listed) creator, ever.
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
