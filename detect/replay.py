#!/usr/bin/env python3
"""Replay CLI — runs the SAME detect/signals/ modules over the whole committed
ledger and prints first-fire time and fires/day per signal. Because run.py and
this script share one engine, live detection and replay cannot drift; the CI
gate asserts their outputs are equal anyway.

Stdout carries timestamps and counts only — never entity keys (public CI logs).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signals import Ctx, evaluate, summary, episodes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def replay(as_of_block=None, data=None):
    ctx = Ctx(root=ROOT, as_of_block=as_of_block, data_dir=data or os.path.join(ROOT, "data"))
    evaluate(ctx)
    return ctx, summary(ctx)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--as-of", type=int, default=None, metavar="BLOCK")
    p.add_argument("--data", default=None)
    p.add_argument("--json", default=None, help="write the summary as JSON here")
    a = p.parse_args()
    ctx, res = replay(a.as_of, a.data)
    print(f"replay: {len(ctx.rows)} rows · mindset {ctx.mindset_source}")
    print(f"{'signal':10s} {'fires':>6s} {'episodes':>8s} {'fires/d':>8s}  {'first fire':16s} {'first page':16s}")
    for sid, r in res.items():
        print(f"{sid:10s} {r['fires']:6d} {r['episodes']:8d} {r['fires_per_day']:8.2f}  "
              f"{r['first_fire'] or '-':16s} {r['first_page'] or '-':16s}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(res, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
