"""#14 rolling-60-min Treasury outflow vs trailing-28-d same-slot median.

FIELD ONLY — never a trigger. Emits no findings; writes the current multiple into
ctx.outflow_x (slot -> multiple) so run.py can stamp it on views and heartbeat.
"""
import collections
import statistics
from . import register, SLOT


@register("outflow", order=55)
def run(ctx):
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
            if med > 0:
                ctx.outflow_x[sl] = round(cv / med, 1)
    return []
