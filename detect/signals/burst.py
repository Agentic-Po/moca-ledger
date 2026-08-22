"""#11 same-recipient equip burst: >= 10 equip-sized Treasury payouts to one
non-excluded recipient within 60 s. Mechanically impossible organically
(organic max 4 / 60 s over the whole recon window)."""
import collections
from . import register, Finding, SLOT, utc


@register("burst", order=11)
def run(ctx):
    T = ctx.thr
    n_min, span = T["burst_n"], T["burst_s"]
    fires = []
    by = collections.defaultdict(list)
    for ts, t, v, bd, tx in ctx.equips:
        if not ctx.is_internal(t):
            by[t].append(ts)
    for t, lst in by.items():
        if len(lst) < n_min:
            continue
        lst.sort()
        i = 0
        last = -1
        for j in range(len(lst)):
            while lst[j] - lst[i] > span:
                i += 1
            k = j - i + 1
            if k >= n_min and lst[i] > last:
                last = lst[j]
                fires.append(Finding("11", t, "page", k, n_min, organic_p95=4, window="60s",
                                     ts=(lst[j] // SLOT) * SLOT,
                                     headline=[f"{k} equip-sized payouts in {span}s to one recipient",
                                               f"burst start {utc(lst[i])} UTC"],
                                     detail=f"{k} in {span}s"))
    return fires
