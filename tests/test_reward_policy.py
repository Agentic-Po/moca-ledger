#!/usr/bin/env python3
"""Regression checks for reward eras, legal cap volume and obsolete pause cases."""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "detect"))
sys.path.insert(1, str(ROOT))
from reward_policy import PAUSED, RESUMED, CAP_STARTED, band
from signals.reward_cap import run as cap
from signals import Ctx
from signals.pause import run as paused_rewards
from run import retire_obsolete_reward_cases


def check(condition):
    if not condition:
        raise AssertionError("reward policy regression")


def main():
    # Future bounded pauses must still be checked, but a completed pause is silent.
    active = SimpleNamespace(thr={"pause_grace_min":30,"pause_burst_n":3},
          pauses=[{"s":1000,"e":10000}], t1=9000, equips=[],
          pay=[(3000,"0xtest",1,"equip","test")], slots=range(3,16),
          in_pause=lambda ts: 1000 <= ts < 10000)
    check(bool(paused_rewards(active)))
    active.t1=11000
    check(not paused_rewards(active))
    u = 100.0
    check(band(100, PAUSED - 1, u) == "equip")
    check(band(10, PAUSED - 1, u) == "invoke")
    check(band(100, RESUMED + 1, u) == "system_topup")
    check(band(100.7, RESUMED + 1, u) == "system_topup")
    check(band(5, RESUMED, u) == "equip")
    check(band(.5, RESUMED, u) == "invoke")
    check(band(5, RESUMED - 1, u) == "other")
    check(band(300, RESUMED, u) == "airdrop")
    check(band(1000, RESUMED, u) == "credit10")
    check(band(1000, RESUMED, 0) == "other")
    start = CAP_STARTED + 24 * 3600
    def ctx(kinds, final=None):
        return SimpleNamespace(pay=[(start + i, "0xtest", 0, k, str(i)) for i,k in enumerate(kinds)],
                               is_internal=lambda _: False, t1=final or start + 3601)
    check(not cap(ctx(["equip"] * 51)))
    check(not cap(ctx(["equip"] * 80)))
    check(not cap(ctx(["invoke"] * 800)))
    check(not cap(ctx(["equip"] * 40 + ["invoke"] * 400)))
    check(not cap(ctx(["system_topup"] * 100 + ["credit10"] * 100)))
    check(cap(ctx(["equip"] * 81))[0].value == 4.05)
    check(cap(ctx(["equip"] * 80 + ["invoke"]))[0].value == 4.005)
    check(not cap(ctx(["equip"] * 81, start + 100)))
    state = {"open": {"old": {"signal":"S-F", "ts": RESUMED + 1, "pending_send":True, "tier":"notify"},
                       "historic": {"signal":"S-F", "ts": PAUSED, "pending_send":True, "tier":"notify"}}}
    retire_obsolete_reward_cases(state)
    check(state["open"]["old"]["status"] == "closed")
    check(not state["open"]["old"]["pending_send"])
    check(state["open"]["historic"]["pending_send"])
    retire_obsolete_reward_cases(state)
    print("reward policy regression: PASS")

if __name__ == "__main__": main()
