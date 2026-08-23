"""#14 rolling-60-min Treasury outflow vs trailing-28-d same-slot median.

FIELD ONLY — never a trigger, and the council cut it from alerting (§5, vote 5: its
own recommended action was "Nothing."). It emits no findings; it writes the current
multiple into ctx.outflow_x (slot -> multiple), which run.py stamps on the heartbeat.
tests/test_gate.py asserts the no-findings part, because a field that quietly starts
firing is exactly the regression this suite exists for.
"""
import collections
import statistics
from . import register, SLOT


@register("outflow", order=55)
def run(ctx):
    T = ctx.thr
    per_slot_v = collections.Counter()
    for ts, t, v, bd, tx in ctx.pay:
        per_slot_v[ts // SLOT] += v
    win = collections.deque()
    cv = 0.0
    hour_v = {}
    ctx.outflow_x = {}
    for sl in ctx.slots:
        win.append(sl)
        cv += per_slot_v.get(sl, 0)
        while win and win[0] <= sl - 6:
            cv -= per_slot_v.get(win.popleft(), 0)
        hour_v[sl] = cv
        hist = [hour_v[x] for x in range(sl - 144, sl - 28 * 144, -144) if x in hour_v]
        if len(hist) >= 7:
            med = statistics.median(hist)
            # A ratio needs a denominator worth dividing by. `med > 0` alone let a
            # same-slot median of ~1e-18 MOCA through and produced a multiple of
            # 8.4e17 — the actual maximum over the committed ledger, with 12 slots
            # above a million. Nothing renders this today, so it was inert; the plan
            # puts it on every view, at which point "outflow is 840 quadrillion times
            # normal" is what a person reads at 03:00. The floor is one payout of the
            # smallest real size: below that the hour was idle and no multiple means
            # anything. Keeps 5,293 of 5,346 slots and caps the maximum at 1,315.
            if med >= T["unit_frozen"] / 10.0:
                ctx.outflow_x[sl] = round(cv / med, 1)
    return []
