"""#10 recipient concentration of equip-sized payouts (rolling 6 h), excluded creators removed.

Tiers:
  #10   PAGE   n>=50: top-1 >= 0.45 or top-3 >= 0.60; "confirmed" escalation at n>=100;
               also PAGE on > 50 equip-sized payouts to one creator / rolling 60 min
  #10n  NOTIFY top-1 >= 0.30 at n>=50
  #10i  DIGEST top-1 >= 0.60 with excluded creators included (drift watch)
"""
import collections
from . import register, Finding, SLOT, H, utc


@register("concentration", order=10)
def run(ctx):
    T = ctx.thr
    win_s = T["conc_window_h"] * H
    fires = []
    w6 = collections.deque()
    c6 = collections.Counter()      # excluded removed
    c6i = collections.Counter()     # everyone
    w1 = collections.deque()
    c1 = collections.Counter()
    eqs = ctx.equips
    i, n = 0, len(eqs)
    organic_top1_p95 = ctx.baselines.get("daily_creator_rewards_organic", {}).get("top1_share_moca", {}).get("p95")

    for sl in ctx.slots:
        end = (sl + 1) * SLOT
        while i < n and eqs[i][0] < end:
            ts, t, v, bd, tx = eqs[i]
            c6i[t] += 1
            w6.append((ts, t))
            if not ctx.is_internal(t):
                c6[t] += 1
                c1[t] += 1
                w1.append((ts, t))
            i += 1
        while w6 and w6[0][0] < end - win_s:
            ts, t = w6.popleft()
            c6i[t] -= 1
            if not ctx.is_internal(t):
                c6[t] -= 1
        while w1 and w1[0][0] < end - H:
            ts, t = w1.popleft()
            c1[t] -= 1

        ni = sum(c6i.values())
        if ni >= T["conc_min_n_confirm"]:
            top, k = c6i.most_common(1)[0]
            if k / ni >= T["conc_top1_internal_digest"]:
                fires.append(Finding("10i", top, "digest", round(k / ni, 3), T["conc_top1_internal_digest"],
                                     window="6h", ts=sl * SLOT, detail=f"top1={k/ni:.0%} n={ni} (all creators)"))
        nn = sum(c6.values())
        if nn >= T["conc_min_n_page"]:
            mc = [x for x in c6.most_common(3) if x[1] > 0]
            sh1 = mc[0][1] / nn
            sh3 = sum(x[1] for x in mc) / nn
            top = mc[0][0]
            paged = False
            if sh1 >= T["conc_top1_page"] or sh3 >= T["conc_top3_page"]:
                which = f"top1={sh1:.0%}" if sh1 >= T["conc_top1_page"] else f"top3={sh3:.0%}"
                esc = "confirmed-n100" if nn >= T["conc_min_n_confirm"] else ""
                fires.append(Finding("10", top, "page", round(sh1, 3), T["conc_top1_page"],
                                     organic_p95=organic_top1_p95, window="6h", ts=sl * SLOT,
                                     headline=[f"{which} of {nn} equip-sized payouts / 6 h",
                                               f"top-1 count {mc[0][1]}",
                                               f"n gate {'confirmed (n>=100)' if esc else 'n>=50'}"],
                                     detail=f"{which} n={nn}", escalation=esc,
                                     evidence=[(utc(ets), ew[:14]) for ets, ew in list(w6)[-40:]]))
                paged = True
            if sh1 >= T["conc_top1_notify"] and not paged:
                fires.append(Finding("10n", top, "notify", round(sh1, 3), T["conc_top1_notify"],
                                     organic_p95=organic_top1_p95, window="6h", ts=sl * SLOT,
                                     detail=f"top1={sh1:.0%} n={nn}"))
        for t, k in c1.items():
            if k > T["conc_per_creator_hour"]:
                fires.append(Finding("10", t, "page", k, T["conc_per_creator_hour"], window="60min", ts=sl * SLOT,
                                     headline=[f"{k} equip-sized payouts to one creator in 60 min"],
                                     detail=f"{k}/60min"))
    return fires
