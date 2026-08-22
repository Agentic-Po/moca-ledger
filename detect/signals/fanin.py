"""Fan-in group.

#4a  NOTIFY  >= 10 Mind->Mind transfers in a rolling 60 min (platform-level)
#4b  NOTIFY  one Mind sink receives from >= 5 distinct Minds / 24 h
#4b-esc PAGE the same sink at >= 10 distinct senders or >= 10,000 MOCA / 24 h
S-C  NOTIFY  a non-Mind, non-system address receives from >= 5 distinct Minds / 24 h
"""
import bisect
import collections
from . import register, Finding, SLOT, DAY, utc


@register("fanin", order=20)
def run(ctx):
    T = ctx.thr
    fires = []
    m2m = []
    fanin_mind = collections.defaultdict(list)
    fanin_nonmind = collections.defaultdict(list)
    for ts, f, t, v, tx, b in ctx.rows:
        if f == t or not ctx.is_mind(f, ts):
            continue
        if ctx.is_mind(t, ts):
            m2m.append(ts)
            fanin_mind[t].append((ts, f, v))
        elif t not in ctx.allow:
            fanin_nonmind[t].append((ts, f, v))

    # #4a platform rate
    per_slot = collections.Counter(ts // SLOT for ts in m2m)
    win = collections.deque()
    cnt = 0
    for sl in ctx.slots:
        win.append(sl)
        cnt += per_slot.get(sl, 0)
        while win and win[0] <= sl - 6:
            cnt -= per_slot.get(win.popleft(), 0)
        if cnt >= T["m2m_rate_hour"]:
            fires.append(Finding("4a", "platform", "notify", cnt, T["m2m_rate_hour"],
                                 organic_p95=ctx.baselines.get("mind_to_mind_per_day", {}).get("p95"),
                                 window="60min", ts=sl * SLOT, detail=f"{cnt} Mind-to-Mind/60min"))

    def sweep(dct, sid, page_eligible):
        k_notify = T["fanin_k_notify"]
        for r, lst in dct.items():
            if len(lst) < k_notify:
                continue
            lst.sort()
            tss = [x[0] for x in lst]
            done = set()
            for i, (ts, f, v) in enumerate(lst):
                sl = ts // SLOT
                if sl in done:
                    continue
                lo = bisect.bisect_left(tss, ts - DAY)
                w = lst[lo:i + 1]
                senders = {x[1] for x in w}
                moca = sum(x[2] for x in w)
                if len(senders) >= k_notify:
                    done.add(sl)
                    esc = page_eligible and (len(senders) >= T["fanin_k_page"] or moca >= T["fanin_moca_page"])
                    fires.append(Finding(sid, r, "page" if esc else "notify",
                                         len(senders), k_notify, window="24h", ts=sl * SLOT,
                                         headline=[f"{len(senders)} distinct Mind senders / 24 h",
                                                   f"{moca:,.0f} MOCA received / 24 h"],
                                         detail=f"{len(senders)} senders / {moca:,.0f} MOCA / 24h"
                                                + (" [esc]" if esc else ""),
                                         escalation="fanin-esc" if esc else "",
                                         evidence=[(utc(x[0]), x[1][:14], round(x[2], 1)) for x in w[-40:]]))

    sweep(fanin_mind, "4b", True)
    sweep(fanin_nonmind, "S-C", False)
    return fires
