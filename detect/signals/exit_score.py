"""#13 scored Mind-wallet outflow — DIGEST only (a sanctioned feature, never blocked).

score = 0.35*taint + 0.25*amount-share + 0.15*big + 0.10*young + 0.10*first-out + 0.05*fresh-dest
Fires (digest) at score >= 0.6.
"""
import collections
from . import register, Finding, SLOT


@register("exit_score", order=50)
def run(ctx):
    T = ctx.thr
    fires = []
    inflow_tot = collections.Counter()
    inflow_taint = collections.Counter()
    cum_out = collections.Counter()
    seen = set()
    from . import TREASURY
    for ts, f, t, v, tx, b in ctx.rows:
        taint_f = inflow_taint[f] / inflow_tot[f] if inflow_tot[f] else 0.0
        if ctx.is_mind(f, ts) and not ctx.is_mind(t, ts) and t not in ctx.allow:
            young = 1 if ctx.age_h(f, ts) <= T["exit_young_d"] * 24 else 0
            first_out = 1 if cum_out[f] == 0 else 0
            cum_out[f] += v
            amt = min(1.0, cum_out[f] / inflow_tot[f]) if inflow_tot[f] else 1.0
            big = 1 if v >= T["exit_min_amt"] else 0
            fresh_dest = 1 if t not in seen else 0
            score = 0.35 * taint_f + 0.25 * amt + 0.15 * big + 0.10 * young + 0.10 * first_out + 0.05 * fresh_dest
            if score >= T["exit_score"]:
                fires.append(Finding("13", f, "digest", round(score, 2), T["exit_score"], window="event",
                                     ts=(ts // SLOT) * SLOT,
                                     detail=f"{v:,.0f} MOCA out, score {score:.2f}"))
        inflow_tot[t] += v
        if f == TREASURY:
            inflow_taint[t] += v
        elif ctx.is_mind(f, ts):
            inflow_taint[t] += v * taint_f
        seen.add(f)
        seen.add(t)
    return fires
