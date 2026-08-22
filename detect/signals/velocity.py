"""Velocity group.

#9  DIGEST (until residue labelled): rolling-60-min first-ever Treasury recipients
    > max(13, p95 of trailing 28 d) for 18 consecutive slots (3 h), per size band;
    credit band suppressed during calendar admin-credit batches.
EV  platform equip velocity: non-excluded equip-sized payouts / rolling 24 h
    >= 2 x trailing-28-d p95 NOTIFY, >= 3 x PAGE (chain-only spread-across-N backstop).
"""
import collections
from . import register, Finding, SLOT, DAY, day_str


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0
    return sorted_vals[min(len(sorted_vals) - 1, int(p * (len(sorted_vals) - 1)))]


@register("velocity", order=40)
def run(ctx):
    T = ctx.thr
    fires = []

    # ---- #9 new-recipient velocity per band
    seen = set()
    per_slot = collections.defaultdict(collections.Counter)
    for ts, t, v, bd, tx in ctx.pay:
        if t not in seen:
            seen.add(t)
            per_slot[ts // SLOT][bd] += 1
            per_slot[ts // SLOT]["all"] += 1
    for bd in ("all", "equip", "airdrop", "other"):
        hist = []
        win = collections.deque()
        cur = 0
        consec = 0
        for sl in ctx.slots:
            n = per_slot.get(sl, {}).get(bd, 0)
            win.append((sl, n))
            cur += n
            while win and win[0][0] <= sl - 6:
                cur -= win.popleft()[1]
            base = hist[-28 * 144:]
            p95 = _pct(sorted(base), 0.95) if len(base) >= 144 else T["newrec_min"]
            thr = max(T["newrec_min"], p95)
            hist.append(cur)
            exempt = bd in ("other", "all") and ctx.cal_exempt(sl * SLOT)
            if cur > thr and not exempt:
                consec += 1
            else:
                consec = 0
            if consec >= T["newrec_consec_slots"]:
                fires.append(Finding("9", f"platform:{bd}", "digest", cur, thr, window="60min x 3h",
                                     ts=sl * SLOT, detail=f"{cur}/h > {thr} for {consec} slots"))

    # ---- EV platform equip velocity (trailing-28-d p95 of daily counts, pause skipped)
    daily = collections.Counter()
    for ts, t, v, bd, tx in ctx.equips:
        if not ctx.is_internal(t):
            daily[day_str(ts)] += 1
    eqs = [e for e in ctx.equips if not ctx.is_internal(e[1])]
    w24 = collections.deque()
    i = 0
    cur_day = None
    p95 = T["ev_p95_default"]
    for sl in ctx.slots:
        end = (sl + 1) * SLOT
        d = day_str(sl * SLOT)
        if d != cur_day:
            cur_day = d
            vals = []
            for j in range(1, 29):
                d_ts = sl * SLOT - j * DAY
                if ctx.in_pause(d_ts):
                    continue
                vals.append(daily.get(day_str(d_ts), 0))
            p95 = max(T["ev_p95_default"], _pct(sorted(vals), 0.95)) if len(vals) >= 14 else T["ev_p95_default"]
        while i < len(eqs) and eqs[i][0] < end:
            w24.append(eqs[i][0])
            i += 1
        while w24 and w24[0] < end - DAY:
            w24.popleft()
        n = len(w24)
        if n >= T["ev_page_mult"] * p95:
            fires.append(Finding("EV", "platform", "page", n, int(T["ev_page_mult"] * p95), organic_p95=p95,
                                 window="24h", ts=sl * SLOT,
                                 headline=[f"{n} equip-sized payouts / 24 h (3x organic p95 {p95})"],
                                 detail=f"{n}/24h >= 3x{p95}"))
        elif n >= T["ev_notify_mult"] * p95:
            fires.append(Finding("EV", "platform", "notify", n, int(T["ev_notify_mult"] * p95), organic_p95=p95,
                                 window="24h", ts=sl * SLOT, detail=f"{n}/24h >= 2x{p95}"))
    return fires
