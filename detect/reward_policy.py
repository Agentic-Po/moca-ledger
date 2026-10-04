"""Timestamped payout sizes; historical rewards keep their original meaning.

The unit supplied by the detector is the inferred MOCA equivalent of $1,
not an equip payment. Size indicates a possible purpose, never verified revenue.
"""
import datetime as dt

PAUSED = int(dt.datetime(2026, 8, 21, 13, 55, tzinfo=dt.UTC).timestamp())
RESUMED = int(dt.datetime(2026, 9, 14, 14, 19, tzinfo=dt.UTC).timestamp())
CAP_STARTED = int(dt.datetime(2026, 9, 14, 20, tzinfo=dt.UTC).timestamp())
EQUIP_USD = 0.05
INVOKE_USD = 0.005
CAP_USD = 4.0


def band(value, ts, dollar_unit):
    if not dollar_unit or dollar_unit <= 0:
        return "other"
    usd = value / dollar_unit
    if ts < PAUSED:
        if 0.07 <= usd <= 0.16:
            return "invoke"
        if 0.8 <= usd <= 1.25:
            return "equip"
    elif ts >= RESUMED:
        if abs(usd / INVOKE_USD - 1) <= 0.120000001:
            return "invoke"
        if abs(usd / EQUIP_USD - 1) <= 0.120000001:
            return "equip"
    if ts >= PAUSED and 0.8 <= usd <= 1.25:
        return "system_topup"
    if 2.5 <= usd <= 3.6:
        return "airdrop"
    # A $10 grant and a purchased credit pack overlap. Keep the size visible
    # without claiming either purpose or assuming an unconfirmed change date.
    if 8.0 <= usd <= 11.0:
        return "credit10"
    return "other"


def reward_usd(kind, ts):
    if ts >= RESUMED:
        return {"equip": EQUIP_USD, "invoke": INVOKE_USD}.get(kind, 0.0)
    if ts < PAUSED:
        return {"equip": 1.0, "invoke": 0.1}.get(kind, 0.0)
    return 0.0
