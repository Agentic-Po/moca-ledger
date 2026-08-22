"""Quest-wallet payout shape (the reward source is monitored, never a suspect).

S-Q   NOTIFY QUEST sends >= 50 / rolling 24 h with fresh (<1 h old) recipient share >= 80 %
      PAGE   QUEST sends >= 100 / rolling 24 h
S-Q2  quest recipients forwarding >= 100 MOCA to a non-system address within 24 h of
      their quest receipt: >= 10 forwards / rolling 24 h NOTIFY, >= 30 PAGE
"""
import collections
from . import register, Finding, QUEST, SLOT, DAY, H, utc


@register("quest", order=35)
def run(ctx):
    T = ctx.thr
    fires = []
    sends = []          # (ts, to, fresh?)
    for ts, f, t, v, tx, b in ctx.rows:
        if f == QUEST:
            fresh = (ts - ctx.first_seen.get(t, ts)) <= T["quest_fresh_h"] * H
            sends.append((ts, t, fresh))
    win = collections.deque()
    n = fr = 0
    i = 0
    done_page = set()
    for sl in ctx.slots:
        end = (sl + 1) * SLOT
        while i < len(sends) and sends[i][0] < end:
            win.append(sends[i])
            n += 1
            fr += 1 if sends[i][2] else 0
            i += 1
        while win and win[0][0] < end - DAY:
            _, _, f0 = win.popleft()
            n -= 1
            fr -= 1 if f0 else 0
        share = fr / n if n else 0
        if n >= T["quest_page_per_day"]:
            fires.append(Finding("S-Q", "platform", "page", n, T["quest_page_per_day"], window="24h",
                                 ts=sl * SLOT,
                                 headline=[f"{n} quest-wallet sends / 24 h",
                                           f"fresh (<1 h) recipient share {share:.0%}"],
                                 detail=f"{n}/24h fresh {share:.0%}"))
        elif n >= T["quest_notify_per_day"] and share >= T["quest_fresh_share"]:
            fires.append(Finding("S-Q", "platform", "notify", n, T["quest_notify_per_day"], window="24h",
                                 ts=sl * SLOT,
                                 headline=[f"{n} quest-wallet sends / 24 h",
                                           f"fresh (<1 h) recipient share {share:.0%}"],
                                 detail=f"{n}/24h fresh {share:.0%}"))

    # S-Q2 pass-through
    quest_recv = ctx.quest_from
    fwd = []            # ts of qualifying forwards
    for ts, f, t, v, tx, b in ctx.rows:
        q = quest_recv.get(f)
        if q is None or ts < q or ts > q + DAY:
            continue
        if t in ctx.allow or v < T["questpt_min_moca"]:
            continue
        fwd.append(ts)
    per_slot = collections.Counter(ts // SLOT for ts in fwd)
    win2 = collections.deque()
    c = 0
    for sl in ctx.slots:
        win2.append(sl)
        c += per_slot.get(sl, 0)
        while win2 and win2[0] <= sl - 144:
            c -= per_slot.get(win2.popleft(), 0)
        if c >= T["questpt_page"]:
            fires.append(Finding("S-Q2", "platform", "page", c, T["questpt_page"], window="24h", ts=sl * SLOT,
                                 headline=[f"{c} quest recipients forwarded >=100 MOCA within 24 h of receipt"],
                                 detail=f"{c}/24h"))
        elif c >= T["questpt_notify"]:
            fires.append(Finding("S-Q2", "platform", "notify", c, T["questpt_notify"], window="24h", ts=sl * SLOT,
                                 detail=f"{c}/24h"))
    return fires
