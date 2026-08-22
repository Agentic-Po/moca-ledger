"""S-A slow harvest, adaptive floor.

Per-creator rolling-24 h equip-sized payouts >=
  max(sa_floor, sa_mult x trailing-28-day organic per-creator-day max)   creator >= 7 d old
  sa_young_floor                                                          creator < 7 d old

"Organic" for the baseline = creators that are not excluded and whose salted hash is not
classed TP/suspect in labels-lite.json. Baseline days inside a reward pause are skipped
and the threshold freezes at its last pre-pause value.
NOTIFY only — a genuinely viral skill can trip this; human review, never auto-act.
"""
import collections
import math
from . import register, Finding, SLOT, DAY, H, day_str, ts_of


@register("slow_harvest", order=25)
def run(ctx):
    T = ctx.thr
    fires = []
    # daily per-creator organic counts for the adaptive baseline
    daily = collections.defaultdict(collections.Counter)   # day -> creator -> n
    for ts, t, v, bd, tx in ctx.equips:
        if ctx.is_internal(t) or ctx.lite_class(t) in ("TP", "suspect"):
            continue
        daily[day_str(ts)][t] += 1
    day_max = {d: max(c.values()) for d, c in daily.items()}

    def thr_for(day_ts):
        vals = []
        for j in range(1, T["sa_baseline_days"] + 1):
            d_ts = day_ts - j * DAY
            if ctx.in_pause(d_ts):
                continue
            vals.append(day_max.get(day_str(d_ts), 0))
        m = max(vals) if vals else 0
        return max(T["sa_floor"], math.ceil(T["sa_mult"] * m)) if m else T["sa_floor"]

    # rolling 24 h per creator, evaluated per slot
    w24 = collections.deque()
    c24 = collections.Counter()
    eqs = ctx.equips
    i, n = 0, len(eqs)
    cur_day = None
    cur_thr = T["sa_floor"]
    for sl in ctx.slots:
        end = (sl + 1) * SLOT
        d = day_str(sl * SLOT)
        if d != cur_day:
            cur_day = d
            cur_thr = thr_for(ts_of(d + "T00:00"))
        while i < n and eqs[i][0] < end:
            ts, t, v, bd, tx = eqs[i]
            if not ctx.is_internal(t):
                c24[t] += 1
                w24.append((ts, t))
            i += 1
        while w24 and w24[0][0] < end - DAY:
            ts, t = w24.popleft()
            c24[t] -= 1
        for t, k in c24.items():
            if k <= 0:
                continue
            young = ctx.age_h(t, end) < 7 * 24
            thr = T["sa_young_floor"] if young else cur_thr
            if k >= thr:
                fires.append(Finding("S-A", t, "notify", k, thr, organic_p95=2, window="24h", ts=sl * SLOT,
                                     headline=[f"{k} equip-sized payouts / rolling 24 h (threshold {thr})",
                                               f"creator age {'<7d' if young else '>=7d'}"],
                                     detail=f"{k}/24h thr={thr}"))
    return fires
