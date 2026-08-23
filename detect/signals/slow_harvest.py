"""S-A slow harvest, adaptive floor.

Per-creator rolling-24 h equip-sized payouts >=
  max(sa_floor, sa_mult x the sa_baseline_pct-ile of organic per-creator-days
      over sa_baseline_days)                                            creator >= 7 d old
  sa_young_floor                                                          creator < 7 d old

"Organic" for the baseline = creators that are not excluded and whose salted hash is not
classed TP/suspect in labels-lite.json. Baseline days inside a reward pause contribute no
creator-days, so a pause shrinks the population rather than the value; with every baseline
day paused the bar is sa_floor.
NOTIFY only — a genuinely viral skill can trip this; human review, never auto-act.
"""
import collections
import math
from . import register, Finding, SLOT, DAY, H, day_str, ts_of, _pct


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
    def thr_for(day_ts):
        """The bar for a creator >= 7 d old on this day.

        A percentile of every organic creator-day in the window, not the largest one.
        Under the max-of-maxima this replaced, ONE creator on ONE day set the bar for
        the next 28 days — and that creator is by construction the most harvest-like
        one nobody has classed yet, so an uncaught farm raised the bar for catching
        farms. labels-lite.json is read by exactly one line of detector code (the
        organic filter above), which made a single wrong class a silent switch for
        this threshold: measured on the committed ledger, dropping every class moved
        the >=7 d bar from 11 to 3,828 (348x) with the floor lifted, and one wallet's
        13 payouts on 2026-07-17 held the live bar at 20 instead of 15 for the 28 days
        up to 14 Aug. The percentile is 1.0x under both, and fires the same 1,174
        times on the same 5 entities."""
        vals = []
        for j in range(1, T["sa_baseline_days"] + 1):
            d_ts = day_ts - j * DAY
            if ctx.in_pause(d_ts):
                continue
            vals.extend(daily.get(day_str(d_ts), {}).values())
        m = _pct(sorted(vals), T["sa_baseline_pct"])
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
