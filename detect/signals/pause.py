"""S-F: a paused reward reappears (combined rule).

After calendar pause start + 30 min grace, while the pause has no end set:
  PAGE   >= 3 equip/invoke-sized Treasury payouts in a rolling 60 min
  PAGE   1 payout to a recipient that had equip-sized receipts before the pause
  NOTIFY 1-2 payouts to first-ever recipients — "verify type: a $1 card payment
         has the same on-chain size" (type_verified is a private-layer upgrade)
Auto-disables when the calendar event has an end timestamp.
"""
import collections
from . import register, Finding, SLOT, H, utc


@register("pause", order=45)
def run(ctx):
    T = ctx.thr
    fires = []
    grace = T["pause_grace_min"] * 60
    active = [c for c in ctx.pauses if c["e"] >= 4102444800]   # no end set -> still active
    if not active:
        return []
    start = min(c["s"] for c in active)
    prior_recips = set()
    for ts, t, v, bd, tx in ctx.equips:
        if ts < start:
            prior_recips.add(t)
    events = []   # (ts, recipient, band, value, first_ever, prior)
    seen_before = set(t for ts, t, v, bd, tx in ctx.pay if ts < start)
    for ts, t, v, bd, tx in ctx.pay:
        if bd not in ("equip", "invoke") or ts < start + grace:
            continue
        events.append((ts, t, bd, v, t not in seen_before, t in prior_recips))
    win = collections.deque()
    i = 0
    for sl in ctx.slots:
        if sl * SLOT < start + grace:
            continue
        end = (sl + 1) * SLOT
        new = []
        while i < len(events) and events[i][0] < end:
            win.append(events[i])
            new.append(events[i])
            i += 1
        while win and win[0][0] < end - H:
            win.popleft()
        if len(win) >= T["pause_burst_n"]:
            fires.append(Finding("S-F", "platform", "page", len(win), T["pause_burst_n"], window="60min",
                                 ts=sl * SLOT,
                                 headline=[f"{len(win)} equip/invoke-sized payouts / 60 min during an active reward pause"],
                                 recommended_action="request an immediate reward-queue stop and review",
                                 detail=f"{len(win)} sized payouts/60min in pause",
                                 evidence=[(utc(x[0]), x[1][:14], x[2], round(x[3], 1)) for x in list(win)[-20:]]))
        for ts, t, bd, v, first_ever, prior in new:
            if prior:
                fires.append(Finding("S-F", t, "page", round(v, 1), None, window="60min", ts=sl * SLOT,
                                     headline=[f"{bd}-sized payout during pause to a recipient with prior equip-sized receipts"],
                                     detail=f"{bd}-sized {v:.0f} MOCA, prior recipient"))
            else:
                fires.append(Finding("S-F", t, "notify", round(v, 1), None, window="60min", ts=sl * SLOT,
                                     headline=[f"{bd}-sized payout during pause, first-ever recipient",
                                               "verify type: a $1 card payment has the same on-chain size"],
                                     detail=f"{bd}-sized {v:.0f} MOCA, first-ever recipient, verify type"))
    return fires
