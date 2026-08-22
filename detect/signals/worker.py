"""#15 payout-worker saturation: >= 1,750 Treasury tx in a rolling 60 min.
The measured worker cap is ~1,800-1,820/h; zero organic hours ever reached this."""
import collections
from . import register, Finding, SLOT


@register("worker", order=15)
def run(ctx):
    T = ctx.thr
    cap = T["worker_hour_cap"]
    fires = []
    per_slot = collections.Counter(ts // SLOT for ts, t, v, bd, tx in ctx.pay)
    win = collections.deque()
    cnt = 0
    for sl in ctx.slots:
        win.append(sl)
        cnt += per_slot.get(sl, 0)
        while win and win[0] <= sl - 6:
            cnt -= per_slot.get(win.popleft(), 0)
        if cnt >= cap:
            fires.append(Finding("15", "platform", "page", cnt, cap, window="60min", ts=sl * SLOT,
                                 headline=[f"{cnt} Treasury tx / rolling 60 min (worker cap ~1800/h)",
                                           "payout worker is saturated"],
                                 recommended_action="request an immediate reward-queue stop and review",
                                 detail=f"{cnt} tx/60min"))
    return fires
