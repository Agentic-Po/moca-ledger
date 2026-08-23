"""INV-#10 / INV-#11 — the #10 and #11 shapes on INVOKE-sized payouts.

An invoke pays about a tenth of an equip, so a farm on the invoke band moves
roughly $170/h even at the payout worker's ceiling. Nothing else on the floor
sees it: #10 and #11 read `ctx.equips` only, and #15 does not trigger below
~1,750 Treasury payouts an hour.

Both ship in SHADOW (`shadow_signals` in thresholds.json). Every fire either of
them produces on the committed ledger belongs to ONE creator that the private
label file classes benign, and invoke rewards have been paused since
2026-08-21 13:51 UTC, so there is no traffic to calibrate against. Page tier on
a shape with zero labelled true positives is a pager for a coin flip.
"""
import collections
from . import register, Finding, SLOT, H, utc


@register("invoke", order=12)
def run(ctx):
    T = ctx.thr
    fires = []
    invokes = ctx.invokes
    win_s = T["conc_window_h"] * H

    # ---- INV-#10. n_min is its OWN gate, not conc_min_n_page: measured on the
    # committed ledger, the equip gate (50) at the same 0.45 share fires on 278
    # slots before the incident begins, because per-creator invoke volume is an
    # order of magnitude thinner than equip volume.
    n_min = T["inv_conc_min_n"]
    w = collections.deque()
    c = collections.Counter()
    i, n = 0, len(invokes)
    for sl in ctx.slots:
        end = (sl + 1) * SLOT
        while i < n and invokes[i][0] < end:
            ts, t, v, bd, tx = invokes[i]
            if not ctx.is_internal(t):
                c[t] += 1
                w.append((ts, t))
            i += 1
        while w and w[0][0] < end - win_s:
            ts, t = w.popleft()
            c[t] -= 1
        nn = sum(c.values())
        if nn < n_min:
            continue
        mc = [x for x in c.most_common(1) if x[1] > 0]
        if not mc:
            continue
        sh1 = mc[0][1] / nn
        if sh1 >= T["inv_conc_top1"]:
            fires.append(Finding("INV-10", mc[0][0], "page", round(sh1, 3), T["inv_conc_top1"],
                                 organic_p95=T.get("inv_conc_organic_p95"), window="6h",
                                 ts=sl * SLOT,
                                 headline=[f"top1={sh1:.0%} of {nn} invoke-sized payouts / 6 h",
                                           f"top-1 count {mc[0][1]}",
                                           f"n gate {n_min}"],
                                 detail=f"top1={sh1:.0%} n={nn}",
                                 evidence=[(utc(ets), ew[:14]) for ets, ew in list(w)[-40:]]))

    # ---- INV-#11. The count is NOT burst_n. Ten invoke payouts to one creator
    # inside 60 s happened organically on 2026-08-06, so the equip band's floor
    # of 10 would put the threshold exactly on the observed organic maximum.
    n_b, span = T["inv_burst_n"], T["burst_s"]
    by = collections.defaultdict(list)
    for ts, t, v, bd, tx in invokes:
        if not ctx.is_internal(t):
            by[t].append(ts)
    for t, lst in by.items():
        if len(lst) < n_b:
            continue
        lst.sort()
        i = 0
        last = -1
        for j in range(len(lst)):
            while lst[j] - lst[i] > span:
                i += 1
            k = j - i + 1
            if k >= n_b and lst[i] > last:
                last = lst[j]
                fires.append(Finding("INV-11", t, "page", k, n_b,
                                     organic_p95=T.get("inv_burst_organic_max"), window="60s",
                                     ts=(lst[j] // SLOT) * SLOT,
                                     headline=[f"{k} invoke-sized payouts in {span}s to one recipient",
                                               f"burst start {utc(lst[i])} UTC"],
                                     detail=f"{k} in {span}s"))
    return fires
