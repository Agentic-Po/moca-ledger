"""Current skill rewards: a wallet over cap is also a per-user lower-bound breach.

Unmapped wallets cannot prove a user's combined total. Clock and rolling
windows are reported separately until the engine's window semantics are known.
Only completed clock hours are eligible for cap notices.
"""
import collections
from . import register, Finding, SLOT, H, utc
from reward_policy import CAP_STARTED, CAP_USD, reward_usd


@register("reward_cap", order=13)
def run(ctx):
    amounts = collections.defaultdict(int)
    for ts, recipient, value, kind, tx in ctx.pay:
        if ts < CAP_STARTED or ctx.is_internal(recipient):
            continue
        # Integer half-cent units avoid a floating-point false breach at $4.
        units = round(reward_usd(kind, ts) * 200)
        if not units:
            continue
        hour = ts // H
        amounts[(recipient, hour)] += units
    fires = []
    for (recipient, hour), units in amounts.items():
        if (hour + 1) * H > ctx.t1 or units <= round(CAP_USD * 200):
            continue
        fires.append(Finding("CAP", recipient, "notify", units / 200, CAP_USD,
            window="completed UTC clock-hour", ts=(hour + 1) * H,
            headline=[f"Skill-sized rewards total ${units / 200:.3f} in a completed clock hour",
                      "wallet-level lower bound; verify payout type and the user's cap window"],
            recommended_action="verify the recorded reward payments and backend cap scope before requesting a pause",
            detail=f"${units / 200:.3f} skill-sized rewards in clock hour beginning {utc(hour * H)} UTC"))
    return fires
