"""Velocity group.

#9  DIGEST (until residue labelled): rolling-60-min first-ever Treasury recipients
    > max(13, p95 of trailing 28 d) for 18 consecutive slots (3 h), per size band;
    credit band suppressed during calendar admin-credit batches.
EV  platform equip velocity: non-excluded equip-sized payouts / rolling 24 h
    >= 2 x trailing-28-d p95 NOTIFY, >= 3 x PAGE (chain-only spread-across-N backstop).
"""
import collections
import statistics
from . import register, Finding, SLOT, DAY, day_str, _pct


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
    # The BASELINE excludes creators already classed TP/suspect, the same filter
    # slow_harvest applies. EV filtered only is_internal, so a labelled farm's own days
    # taught EV what normal looks like. The measured counter has to stay unfiltered —
    # it is what is happening now, not what is ordinary — so the two are built apart.
    daily = collections.Counter()          # BASELINE: what ordinary days look like
    daily_all = collections.Counter()      # MEASURED: everything that is not internal
    for ts, t, v, bd, tx in ctx.equips:
        if ctx.is_internal(t):
            continue
        daily_all[day_str(ts)] += 1
        if ctx.lite_class(t) not in ("TP", "suspect"):
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
            # A "p95" over 28 points IS the second-largest value — for n=28 the index is
            # 25 of 0..27 — so three incident days in the window make the baseline an
            # outlier picker rather than a percentile. Measured 2026-08-23 with 19-21 Aug
            # inside the window: the sorted series was [2,3,4,...,69,69,205,4247,17015],
            # p95 landed on 205, and EV's page bar became 615 equip payouts a day against
            # a busiest-organic-day-ever of 124. The one platform-wide backstop against a
            # spread operator was 5x looser than the traffic it watches, and replay cannot
            # show it because replay evaluates the historic per-slot threshold.
            # Same disease as S-A's max-of-maxima: a signal learning its bar from the
            # attack. Bounded by a multiple of the window's own median, which contamination
            # cannot move: the cap rises only when ORGANIC volume genuinely rises.
            if len(vals) >= 14:
                srt = sorted(vals)
                base = min(_pct(srt, 0.95), T["ev_baseline_cap_mult"] * statistics.median(srt))
                p95 = max(T["ev_p95_default"], base)
            else:
                p95 = T["ev_p95_default"]
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
