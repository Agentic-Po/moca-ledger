"""S-G persistent watchlist touch (salted-hash watchlist, detect/watchlist.json).

NOTIFY on: first touch per entity per 24 h, any touch >= 1,000 MOCA, or an
outflow to a non-system counterparty never seen before by that entity.
Everything else -> DIGEST residue.
"""
import collections
from . import register, Finding, SLOT, DAY, utc


@register("watchlist", order=30)
def run(ctx):
    if not ctx.watch_hashed or not ctx.salt:
        return []
    T = ctx.thr
    fires = []
    last_notify = {}                       # entity -> ts of last notify-tier touch
    seen_counterparty = collections.defaultdict(set)
    for ts, f, t, v, tx, b in ctx.rows:
        for w, is_out in ((f, True), (t, False)):
            if w in ctx.allow or not ctx.on_watch(w):
                continue
            other = t if is_out else f
            reasons = []
            if ts - last_notify.get(w, -10 ** 12) >= DAY:
                reasons.append("first touch / 24 h")
            if v >= T["watch_big_moca"]:
                reasons.append(f"{v:,.0f} MOCA")
            if is_out and other not in ctx.allow and other not in seen_counterparty[w]:
                reasons.append("new outbound counterparty")
            seen_counterparty[w].add(other)
            if reasons:
                last_notify[w] = ts
                fires.append(Finding("S-G", w, "notify", round(v, 1), None, window="24h",
                                     ts=(ts // SLOT) * SLOT,
                                     headline=[f"watchlist {'outflow' if is_out else 'inflow'} {v:,.0f} MOCA",
                                               "; ".join(reasons)[:80]],
                                     detail=f"{'out' if is_out else 'in'} {v:,.0f} ({'; '.join(reasons)})",
                                     evidence=[(utc(ts), f[:14], t[:14], round(v, 1))]))
            else:
                fires.append(Finding("S-G", w, "digest", round(v, 1), None, window="24h",
                                     ts=(ts // SLOT) * SLOT,
                                     detail=f"{'out' if is_out else 'in'} {v:,.0f}"))
    return fires
