#!/usr/bin/env python3
"""Plain-English layer for alerts.

An alert is read by whoever is awake, not by whoever built the detector. Every
message therefore opens with a sentence in ordinary Minds vocabulary (Skill,
equip, creator, Mind, reward), says why it matters in money or trust terms, and
says what to do. Signal codes and timing go in a footer for the record.

Vocabulary rules kept deliberately: a builder publishes a **Skill**; a user
**equips** then **invokes** it; the Treasury pays the creator ~$1 per equip and
~$0.10 per invoke; a **Mind** is a user's agent and each Mind has a wallet.

Two rules the copy in this file obeys:
  - **Measured is a sentence, never a formula.** `top1=0.62 n=113` tells the
    reader nothing; "Of 113 reward payouts in the last 6 hours, 71 — 62% — went
    to this one creator" tells them everything.
  - **The trigger value and the organic p95 never appear.** What "normal" looks
    like is said in words instead. One forwarded screenshot must not hand the
    operator the exact constraint they have to stay under.
"""
import datetime as dt
import json
import os
import re
import sys

# title · what happened (format string) · why it matters · what to do ·
# normal (what ordinary activity looks like, in words — never the trigger value)
SIGNALS = {
    "10": {
        "title": "One creator is taking an unusual share of reward payouts",
        "what": "One creator wallet is receiving far more of the equip rewards than any single creator normally does.",
        "why": "Every equip pays about $1 from the Treasury. Two very different things look identical on-chain here: a Skill that is genuinely popular, and accounts equipping a Skill the same person controls. Nothing in this alert can tell them apart — that check has not been made.",
        "do": "Look at who is equipping this creator's Skills. If the equippers are brand-new Minds with no other activity, ask for this creator's rewards to be paused.",
        "normal": "A normal top creator takes about a tenth of the equip rewards in a window like this.",
    },
    # 10n is the same measurement as 10 at a lower share; 10i is the same again
    # but counting our own creator wallets, which is why it is context, not a page.
    "10n": {
        "title": "One creator is taking a large share of reward payouts",
        "what": "One creator wallet is taking a bigger share of the equip rewards than a single creator usually does — not yet at the level that pages someone.",
        "why": "Every equip pays about $1 from the Treasury. A genuinely popular Skill and a Skill being equipped by accounts the same person controls look identical on-chain, and that check has not been made.",
        "do": "Note the wallet. If it appears again, or a burst or collection wallet shows up alongside it, treat it as the real thing.",
        "normal": "A normal top creator takes about a tenth of the equip rewards in a window like this.",
    },
    "10i": {
        "title": "One creator took a large share of reward payouts (our own creators counted in)",
        "what": "Counting every creator wallet, including the ones we run ourselves, one of them took most of the equip rewards in this window.",
        "why": "Our own creator wallets are excluded from the alerting version of this check, so this one is context: it says the concentration exists, not that anyone is farming.",
        "do": "Nothing on its own. It matters if the same wallet shows up in an alert that excludes our own creators.",
        "normal": "A normal top creator takes about a tenth of the equip rewards in a window like this.",
    },
    "11": {
        "title": "A creator got a burst of reward payouts in seconds",
        "what": "A single creator wallet received ten or more equip rewards within one minute.",
        "why": "A person cannot equip ten Skills in a minute. This is automated equipping — the fastest form of reward farming, and the clearest sign money is leaving the Treasury right now.",
        "do": "Treat as live. Ask for this creator's rewards to be paused, then review the equipping Minds.",
        "normal": "A busy real creator picks up a few equips an hour, spread out — not ten inside a minute.",
    },
    "15": {
        "title": "The Treasury is paying out as fast as it physically can",
        "what": "The payout system has a hard ceiling of roughly 1,800 payouts an hour, and it is sitting on that ceiling right now.",
        "why": "The payout worker only saturates when something is requesting rewards far faster than real users generate them. On 21 Aug this state coincided with the largest hour of loss.",
        "do": "Stop first, investigate second. Ask for equip and invoke rewards to be paused platform-wide until the cause is known.",
        "normal": "A busy normal day produces about 125 Treasury payouts in an hour.",
    },
    "4b": {
        "title": "Many Minds are sending their MOCA into one wallet",
        "what": "One wallet is collecting MOCA sent in by many different Minds.",
        "why": "Ordinary users do not pay each other. A collection point is how farmed rewards are gathered before being sold, so this usually names the wallet worth freezing.",
        "do": "Check whether the collecting wallet belongs to a real builder. If not, note the address — it is the one to give an exchange if funds move on.",
        "normal": "Ordinary Minds barely pay each other at all, so one wallet collecting from several of them in a day is not a shape normal use produces.",
    },
    "4a": {
        "title": "Minds are transferring MOCA to each other unusually often",
        "what": "Minds are sending MOCA to each other far more often than usual.",
        "why": "There is no product reason for Minds to pay each other. A spike normally means one operator is shuffling rewards between wallets they own.",
        "do": "No action on its own — watch for a collection wallet to appear next.",
        "normal": "On a normal day Minds pay each other only a handful of times an hour.",
    },
    "S-C": {
        "title": "Many Minds are sending MOCA to an outside wallet",
        "what": "An address that is not a Mind is receiving MOCA from several different Minds.",
        "why": "This is the same consolidation pattern as above, but the destination sits outside the platform entirely — usually one step before a sale.",
        "do": "Note the address. If it starts moving funds to an exchange, it belongs in a freeze request.",
        "normal": "Money normally leaves the platform one Mind at a time, to a different address each time.",
    },
    "S-A": {
        "title": "A creator is earning steadily above what real usage produces",
        "what": "One creator is earning equip rewards steadily, above the level real usage normally produces.",
        "why": "Slower than a burst and easy to miss, but the same pattern: sustained earning without matching real usage. This is what a patient operator looks like.",
        "do": "Compare this creator's equips against actual Skill usage. A popular new Skill can look like this — check before acting.",
        "normal": "A creator of this age normally takes a couple of equip rewards a day.",
    },
    "S-B": {
        "title": "Several creators are feeding one wallet and together taking most of the rewards",
        "what": "Several different creator wallets are passing their MOCA on to the same collecting wallet, and between them they took most of the equip rewards in this window — more than any one of them reached on its own. Some of these wallets may already have alerts of their own; what is new here is that they are connected.",
        "why": "This is what spreading out looks like. Every other check on this floor scores creators one at a time, so farming split across several accounts can sit below the single-creator line on each of them; the wallet they all pay into is what puts them back together. Nothing here says the accounts belong to one person: the only thing measured is that MOCA from all of them lands on the same address within two steps.",
        "do": "Start with the collecting wallet. If it is not a real builder's wallet, that is the address to hand an exchange, and each creator paying into it is worth reviewing. If they turn out to be unrelated people who used the same exchange or bridge, say so and that address goes on the allow list.",
        "normal": "Different creators normally cash out to different places; several of them funnelling into one address in the same week is not a shape ordinary use produces.",
    },
    "S-G": {
        "title": "A wallet we are already watching just moved",
        "what": "A wallet connected to the August incident has moved MOCA again.",
        "why": "This wallet is on the watch list from the August incident. Movement means the operator is active again.",
        "do": "A follow-up message with the account detail may arrive underneath this one. Read it, then decide if this needs escalating.",
        "normal": "These wallets have been quiet since August — that is why they are on the list.",
    },
    "S-Q": {
        "title": "Quest rewards are being claimed unusually fast",
        "what": "Quest rewards are going out much faster than usual, mostly to Minds created in the last hour.",
        "why": "Quest rewards are meant for new users completing onboarding. A surge of brand-new Minds claiming them is how a farm builds its inventory before using it.",
        "do": "Check whether the new accounts look genuine — same email domains, same IP, created minutes apart are all warning signs.",
        "normal": "Quest rewards normally go out at a steady trickle, to accounts of all ages.",
    },
    "S-Q2": {
        "title": "Quest rewards are being forwarded straight out",
        "what": "Minds are forwarding their quest reward to another wallet within a day of receiving it.",
        "why": "A genuine new user spends their quest reward on the platform. Immediately forwarding it means the account exists only to collect.",
        "do": "Note where the funds are going — the destination is usually the operator's collection wallet.",
        "normal": "A genuine new user spends the quest reward on the platform instead of passing it on.",
    },
    "S-F": {
        "title": "A reward payout happened while rewards are supposed to be paused",
        "what": "A payout the size of an equip or invoke reward went out after rewards were paused.",
        "why": "Equip and invoke rewards were paused on 21 Aug. A payout in that shape means either the pause has a gap, or this is a different kind of payment that looks similar on-chain.",
        "do": "Ask the platform team to confirm what this payment actually was before assuming the pause failed.",
        "normal": "While the pause holds, nothing of this size should be leaving the Treasury at all.",
    },
    "S-X": {
        "title": "Money moved in a wallet linked to the August incident",
        "what": "The balance changed in a wallet that handled proceeds from the August farming.",
        "why": "These wallets held or moved the proceeds of the August farming. Movement now means funds are being shifted, possibly toward an exchange.",
        "do": "If the destination is an exchange deposit address, that is the moment a freeze request has the best chance of working.",
        "normal": "These balances have sat still since August.",
    },
    "EV": {
        "title": "Platform-wide equip rewards are far above normal",
        "what": "Equip rewards across the whole platform are far above their normal daily level.",
        "why": "This catches farming that is spread across many creators so that no single one looks unusual — the total still gives it away.",
        "do": "Check whether the growth is spread across many genuine creators or concentrated in a handful of new ones.",
        "normal": "A normal day's equip rewards stay inside a much narrower range than this.",
    },
    "9": {
        "title": "Unusually many wallets are receiving Treasury payouts for the first time",
        "what": "An unusual number of wallets received a Treasury payout for the very first time.",
        "why": "New recipients arriving in bulk usually means new accounts were created in bulk.",
        "do": "Normal during a campaign. If there is no campaign running, look at how those accounts were created.",
        "normal": "First-time recipients normally arrive in a trickle through the day, not in a block.",
    },
    "INV-10": {
        "title": "One creator is taking an unusual share of the smaller usage rewards",
        "what": "One creator wallet is receiving far more of the invoke-sized reward payouts than any single creator normally does.",
        "why": "An invoke pays about a tenth of what an equip pays, so this route moves money roughly ten times more slowly and none of the equip-side checks look at it. A genuinely popular Skill and a Skill being invoked by accounts the same person controls are identical on-chain, and that check has not been made.",
        "do": "Read this next to the equip-side alerts on the same wallet. On its own it is a new check whose normal range has not been measured on live traffic, so treat it as a lead, not a finding.",
        "normal": "On the pre-pause ledger the busiest single creator took about a fifth of the invoke-sized payouts in a window like this.",
    },
    "INV-11": {
        "title": "A creator got a burst of the smaller usage rewards in seconds",
        "what": "A single creator wallet received a run of invoke-sized reward payouts inside one minute.",
        "why": "A person cannot invoke that many Skills in a minute. On the equip band the same shape is the clearest sign money is leaving the Treasury right now; on the invoke band it is the same shape at a tenth of the value, and it has not been seen often enough to know its normal range.",
        "do": "Check whether the same wallet has anything open on the equip-side checks. Alone, this is a lead to record, not a reason to wake anyone.",
        "normal": "The busiest minute any creator had on the pre-pause ledger, outside the August incident, was ten invoke-sized payouts.",
    },
    "13": {
        "title": "A Mind moved MOCA out to an outside wallet",
        "what": "A Mind sent MOCA to an address that is not part of the platform.",
        "why": "Withdrawing is a normal, allowed feature — this is recorded for the trail, not because it is wrong on its own.",
        "do": "Nothing, unless this wallet also appears in another alert.",
        "normal": "Withdrawing is a normal, allowed feature and happens every day.",
    },
}

FALLBACK = {
    "title": "Unusual activity in the reward flow",
    # Not "{value}": that repeated the Measured line word for word two lines above it.
    "what": "This signal has no hand-written description yet, so the measurement below is all there is.",
    "why": "This pattern is outside what normal platform activity produces.",
    "do": "Read the measurement below. A follow-up message with more detail may arrive underneath it.",
}

TIER_WORD = {
    "page":   ("🚨", "Needs attention now"),
    "notify": ("🟠", "Worth a look today"),
    "digest": ("📋", "For the record"),
}

# The loudest instruction in the message is the one people use. Make it the one
# that records a decision — not /ack, which was the mute button (council §5).
REPLY_LINE = "<b>Reply to this message with:</b>  contained · reported · watching · closed"

# The digest's shadow heading, as a constant so a test asserts the exact words the
# reader sees rather than a paraphrase of them.
SHADOW_HEADING = "🔭 <b>In shadow — recorded, deliberately not paging</b>"

# HEDGE (defined with the money helpers below) is the one wording every payout
# and money line in this channel uses.


# ---------------------------------------------------------------- small helpers

def _int(x, default=None):
    try:
        return int(round(float(x)))
    except (TypeError, ValueError):
        return default


def _num(x, dp=0):
    try:
        return f"{float(x):,.{dp}f}"
    except (TypeError, ValueError):
        return str(x)


WINDOW_WORDS = {
    "60s": "one minute",
    "60min": "the last hour",
    "6h": "the last 6 hours",
    "24h": "the last 24 hours",
    "60min x 3h": "an hour",
    "event": "one transfer",
    "slot": "the last few minutes",
}


def _window(f):
    w = str(f.get("window") or "").strip()
    return WINDOW_WORDS.get(w, f"the last {w}" if w else "the window")


# Anything that hands over the exact constraint. Applied to text the detector
# wrote, which is allowed to be precise because it was never meant for Telegram.
_LEAK = (
    re.compile(r"\s*\(threshold[^)]*\)", re.I),
    re.compile(r"\s*\bthr\s*=\s*[\d.]+", re.I),
    re.compile(r"\s*\(?\s*\d+\s*x\s*organic p95\s*[\d.,]+\s*\)?", re.I),
    re.compile(r"\s*\borganic p95\s*[\d.,]+", re.I),
    re.compile(r"\s*>=\s*\d+\s*x\s*[\d.,]+"),
    re.compile(r"\s*\(the level that triggers a look:[^)]*\)", re.I),
)


def _scrub(text):
    t = str(text or "")
    for rx in _LEAK:
        t = rx.sub("", t)
    return t.strip(" ·,-")


def _ordinal(n):
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _scope_word(f):
    k = str(f.get("key") or "")
    if k.startswith("0x") or k.startswith("exit:"):
        return "this wallet"
    if k.startswith("platform"):
        return "the platform"
    return "this case"


def _as_of(f):
    try:
        return dt.datetime.fromtimestamp(int(f.get("ts")), dt.UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


# ------------------------------------------------- Measured, one signal at a time
# Each builder returns ONE sentence or None. None falls through to the detector's
# own headline sentence, then to the raw detail. No builder prints the threshold.

def _m_conc(f):                                   # 10 / 10i / 10n
    d = str(f.get("detail") or "")
    m = re.search(r"top(1|3)=(\d+)%\s*n=(\d+)", d)
    if m:
        which, pct, n = m.group(1), int(m.group(2)), int(m.group(3))
        k = int(round(n * pct / 100.0))
        who = "this one creator wallet" if which == "1" else "the top three creator wallets between them"
        scope = " (our own creator wallets counted in)" if str(f.get("signal", "")).lstrip("#") == "10i" else ""
        return f"Of {n:,} reward payouts in {_window(f)}, {k:,} — {pct}% — went to {who}{scope}."
    m = re.fullmatch(r"(\d+)/60min", d.strip())
    k = int(m.group(1)) if m else _int(f.get("value"))
    if k is not None and str(f.get("window") or "") == "60min":
        return f"This one creator wallet received {k:,} equip-sized payouts in the last hour."
    return None


def _m_burst(f):                                  # 11
    m = re.fullmatch(r"(\d+) in (\d+)s", str(f.get("detail") or "").strip())
    if not m:
        return None
    return (f"{int(m.group(1))} equip-sized payouts landed on this one creator wallet "
            f"inside {int(m.group(2))} seconds.")


def _m_inv_conc(f):                               # INV-10
    m = re.search(r"top1=(\d+)%\s*n=(\d+)", str(f.get("detail") or ""))
    if not m:
        return None
    pct, n = int(m.group(1)), int(m.group(2))
    k = int(round(n * pct / 100.0))
    return (f"Of {n:,} invoke-sized reward payouts in {_window(f)}, {k:,} — {pct}% — went to "
            f"this one creator wallet.")


def _m_inv_burst(f):                              # INV-11
    m = re.fullmatch(r"(\d+) in (\d+)s", str(f.get("detail") or "").strip())
    if not m:
        return None
    return (f"{int(m.group(1))} invoke-sized payouts landed on this one creator wallet "
            f"inside {int(m.group(2))} seconds.")


def _m_worker(f):                                 # 15
    n = _int(f.get("value"))
    return f"The Treasury sent {n:,} payouts in the last hour." if n is not None else None


def _m_m2m(f):                                    # 4a
    n = _int(f.get("value"))
    return f"Minds sent MOCA to other Minds {n:,} times in the last hour." if n is not None else None


def _m_fanin(f):                                  # 4b / S-C
    outside = str(f.get("signal", "")).lstrip("#") == "S-C"
    dest = "one address that is not a Mind" if outside else "this one wallet"
    m = re.search(r"(\d+) senders / ([\d,]+) MOCA / 24h", str(f.get("detail") or ""))
    if m:
        return (f"{int(m.group(1))} different Minds sent MOCA into {dest} over the last 24 hours — "
                f"{m.group(2)} MOCA in total.")
    n = _int(f.get("value"))
    return f"{n} different Minds sent MOCA into {dest} over the last 24 hours." if n is not None else None


def _m_slow(f):                                   # S-A
    n = _int(f.get("value"))
    if n is None:
        return None
    return (f"This one creator wallet took {n} equip-sized payouts over the last 24 hours, "
            f"a few at a time rather than in a burst.")


def _m_watch(f):                                  # S-G
    m = re.match(r"\s*(in|out)\s+([\d,]+)", str(f.get("detail") or ""))
    if m:
        verb = "received" if m.group(1) == "in" else "sent out"
        return f"A wallet on the August watch list {verb} {m.group(2)} MOCA in the last 24 hours."
    v = f.get("value")
    if v is None:
        return None
    return f"A wallet on the August watch list moved {_num(v)} MOCA in the last 24 hours."


def _m_quest(f):                                  # S-Q
    m = re.fullmatch(r"(\d+)/24h fresh (\d+)%", str(f.get("detail") or "").strip())
    if m:
        return (f"{int(m.group(1))} quest reward payouts went out in the last 24 hours, and "
                f"{int(m.group(2))}% of them went to Minds created in the hour before they were paid.")
    n = _int(f.get("value"))
    return f"{n} quest reward payouts went out in the last 24 hours." if n is not None else None


def _m_questpt(f):                                # S-Q2
    n = _int(f.get("value"))
    if n is None:
        return None
    return (f"{n} Minds that had just been paid a quest reward forwarded it straight on to another "
            f"wallet within a day.")


def _m_pause(f):                                  # S-F
    d = str(f.get("detail") or "")
    m = re.match(r"(\d+) sized payouts/60min in pause", d)
    if m:
        return (f"{int(m.group(1))} payouts the size of an equip or invoke reward went out in the last "
                f"hour, while rewards are supposed to be paused.")
    m = re.match(r"([A-Za-z]+)-sized ([\d,]+) MOCA, (first-ever|prior) recipient", d)
    if m:
        seen = ("a wallet that has never been paid before" if m.group(3) == "first-ever"
                else "a wallet that has been paid before")
        return (f"A payout of {m.group(2)} MOCA — the size of an {m.group(1)} reward — went out to "
                f"{seen}, while rewards are supposed to be paused.")
    return None


def _m_balance(f):                                # S-X
    parts = [p.strip() for p in str(f.get("detail") or "").split(";") if p.strip()]
    m = re.match(r"([A-Za-z]+)\s+([+-][\d,.]+)\s*->\s*([\d,.]+)", parts[0]) if parts else None
    if not m:
        return None
    sym, delta, newbal = m.group(1).upper(), m.group(2), m.group(3)
    verb = "took in" if delta.startswith("+") else "sent out"
    extra = f" ({len(parts) - 1} other balance move{'s' if len(parts) > 2 else ''} on the same wallet)" \
        if len(parts) > 1 else ""
    return (f"A wallet that handled money from the August farming {verb} {delta.lstrip('+-')} {sym}, "
            f"leaving {newbal} {sym}{extra}.")


def _m_newrec(f):                                 # 9
    band = str(f.get("key") or "").split(":")[-1]
    word = {"equip": "equip-sized", "airdrop": "airdrop-sized", "other": "other-sized"}.get(band, "Treasury")
    m = re.match(r"(\d+)/h\s*>\s*[\d.]+\s*for\s*(\d+) slots", str(f.get("detail") or ""))
    if m:
        hours = int(m.group(2)) * 10 / 60.0
        span = f"about {hours:.0f} hours" if hours >= 1.5 else "more than an hour"
        return (f"{int(m.group(1))} wallets received their first-ever {word} payout inside an hour, "
                f"and it stayed that high for {span}.")
    n = _int(f.get("value"))
    return f"{n} wallets received their first-ever {word} payout inside an hour." if n is not None else None


def _m_velocity(f):                               # EV
    """No multiple. "about three times what a normal day produces" alongside the
    measured value solves for the baseline AND the multiplier, which is the exact
    constraint this module exists to keep out of a forwardable channel."""
    n = _int(f.get("value"))
    if n is None:
        return None
    return (f"Across the whole platform, {n:,} equip-sized payouts went out in 24 hours \u2014 "
            f"well above an ordinary day.")


def _m_exit(f):                                   # 13
    m = re.match(r"([\d,]+) MOCA out", str(f.get("detail") or ""))
    if not m:
        return None
    return f"A Mind sent {m.group(1)} MOCA to an address outside the platform."


def _m_composite(f):                              # composite
    """Count and shape, never the list of codes.

    "10, S-A" is unreadable to the colleague this channel exists for, and to the
    operator it is a map of which detectors are live."""
    n = _int(f.get("value"))
    if n is None:
        return None
    return (f"{n} different alerts fired on wallets connected to each other within a few "
            f"hours \u2014 the alerts are listed on the individual messages for those wallets.")


def _sinks_words(csv_addrs, n_sinks):
    """The collecting addresses, as <code> so a reader can copy one straight into a
    freeze request from a phone. Capped at two: the sentence is the alert's only
    actionable line and a wall of addresses is not read."""
    addrs = [a for a in str(csv_addrs or "").split(",") if a]
    if not addrs:
        return ""
    shown = " and ".join(f"<code>{a}</code>" for a in addrs[:2])
    rest = n_sinks - min(len(addrs), 2)
    lead = "The wallet they all pay into is" if n_sinks == 1 else "They pay into"
    return f"{lead} {shown}" + (f", and {rest} more" if rest > 0 else "") + "."


def _m_cluster(f):                                # S-B
    """The group, its share, and the address a person can act on.

    The member wallets are deliberately absent. The finding establishes that MOCA
    from several creators lands on one address — not that one person owns them —
    and a forwarded list of creator wallets reads as an accusation of the people
    behind them."""
    d = str(f.get("detail") or "").strip()
    m = re.match(r"cluster6h (\d+)/(\d+) m=(\d+) top=(\d+) s=(\d+)(?: sinks=(\S+))?$", d)
    if m:
        k, n, mem, top, ns = (int(x) for x in m.groups()[:5])
        return (f"{mem} creator wallets that all pass their MOCA on to the same collecting wallet "
                f"took {k:,} of {n:,} reward payouts between them in {_window(f)} — {k/n:.0%}. "
                f"The largest single wallet in the group took {top:,} of them. "
                + _sinks_words(m.group(6), ns))
    m = re.match(r"cluster24h (\d+) m=(\d+) top=(\d+) s=(\d+)(?: sinks=(\S+))?$", d)
    if m:
        k, mem, top, ns = (int(x) for x in m.groups()[:4])
        return (f"{mem} creator wallets that all pass their MOCA on to the same collecting wallet "
                f"took {k:,} reward payouts between them in {_window(f)}, and no single one of them "
                f"took more than {top:,}. " + _sinks_words(m.group(5), ns))
    return None


MEASURED = {
    "10": _m_conc, "10i": _m_conc, "10n": _m_conc,
    "11": _m_burst, "INV-10": _m_inv_conc, "INV-11": _m_inv_burst,
    "15": _m_worker, "4a": _m_m2m, "4b": _m_fanin, "S-C": _m_fanin,
    "S-A": _m_slow, "S-G": _m_watch, "S-Q": _m_quest, "S-Q2": _m_questpt,
    "S-F": _m_pause, "S-X": _m_balance, "9": _m_newrec, "EV": _m_velocity,
    "13": _m_exit, "composite": _m_composite, "S-B": _m_cluster,
}


def measured(f):
    """The Measured line, as a sentence a non-technical reader can act on.

    Order: the hand-written sentence for this signal, then the detector's own
    readable headline, then the raw detail. Never the threshold, never the p95.
    """
    sig = str(f.get("signal", "")).lstrip("#")
    fn = MEASURED.get(sig)
    if fn:
        try:
            s = fn(f)
        except Exception as ex:          # a broken builder must not be invisible
            print(f"explain: measured({sig}) failed: {ex!r}", file=sys.stderr)
            s = None
        if s:
            return s
    head = [h for h in (f.get("headline") or []) if h]
    if head:
        return _scrub(head[0])
    if f.get("detail"):
        return _scrub(f["detail"])
    v = f.get("value")
    return f"Measured value {v}." if v is not None else "No measurement was recorded."


_TYPED = re.compile(r"\b(reward|rewards|payout|payouts|equip|equips|invoke|quest)\b", re.I)


def _inferred_note(f, sentence):
    """Payment types are guessed from the size of the transfer on-chain
    (`type_verified` is False on every finding). Say so wherever the sentence
    calls something a reward or a payout."""
    if f.get("type_verified") is True:
        return None
    if not _TYPED.search(str(sentence or "")):
        return None
    return f"<i>{HEDGE}.</i>"


def _esc(x):
    """A human's own note comes back out inside parse_mode=HTML. A note containing
    `<` makes Telegram answer 400 "can't parse entities" and the whole page fails;
    a note containing an <a href> injects a clickable link into every rendering."""
    return str(x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _normal_for(f, spec):
    """What ordinary activity looks like, in the SAME units as the Measured sentence.

    Signal 10 has two variants: a share ("62% of payouts") and a count ("148 payouts
    in the last hour"). One `normal` phrased as a share sat under both, so the count
    variant asked the reader to compare 148 against "about a tenth" of an unstated
    total."""
    sig = str(f.get("signal", "")).lstrip("#")
    if sig in ("10", "10i", "10n") and str(f.get("window") or "") == "60min":
        return "A busy real creator picks up a few equip rewards an hour, not dozens."
    return spec.get("normal")


def _grew_words(f, verb="marked it contained"):
    """How much it has moved since a human last judged it, WITHOUT bare numbers.

    "It was 148 when you marked it; it is 302 now" gives a reader with no units no
    way to tell whether that is three times worse or eleven percent worse \u2014 and the
    Measured sentence two lines below already carries the same quantity in words."""
    try:
        now, base = float(f.get("value")), float(f.get("value_at_status"))
        if base > 0 and now > 0:
            r = now / base
            if r >= 1.15:
                return f"It is now about <b>{r:.1f}\u00d7</b> what it was when you {verb} \u2014 see the measurement below."
            if r <= 0.85:
                return f"It is now lower than when you {verb}, but still active \u2014 see the measurement below."
            return f"It is at about the same level as when you {verb} \u2014 see the measurement below."
    except (TypeError, ValueError):
        pass
    return f"The measurement below is where it stands now, against when you {verb}."


def _footer(f):
    """Timing and recurrence, not the constraint. `alert_seq` is the one small
    integer notify/telegram.py stamps per send (see critic #2 on state size)."""
    bits = [f"signal {f.get('signal')}"]
    as_of = _as_of(f)
    if as_of:
        bits.append(f"as of {as_of:%H:%M} UTC")
    first = str(f.get("episode_first") or f.get("first_ts") or "").strip()
    if first:
        same_day = as_of is not None and first[:10] == f"{as_of:%Y-%m-%d}" and len(first) >= 16
        bits.append(f"first seen {first[11:16] if same_day else first}")
    seq = f.get("alert_seq")
    if isinstance(seq, int) and seq > 0:
        bits.append(f"{_ordinal(seq)} alert on {_scope_word(f)}")
    return "<i>" + " · ".join(bits) + "</i>\n" + REPLY_LINE


# ------------------------------------------------------------------ money
# `phase3 §1.2` asks every page to carry the money. detect/run.py stores two
# small numbers per finding — moca_since (MOCA paid from the Treasury to this
# wallet since the finding was first seen) and rate_per_h — and detect/price.py
# holds a once-a-day USD price cached in the repo. Both are optional: a finding
# whose value is a share rather than an amount has no total, and a price feed
# that is down costs the dollar figure, not the alert.

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Rides with every money figure. detect/run.py stamps type_verified = False on
#: every finding: what these payments actually were is inferred from their size
#: on-chain and has never been confirmed by the platform. A bare total reads as
#: an accusation, and one forwarded accusation is unrecoverable (council §6.4).
HEDGE = "payment type inferred from size on-chain — unconfirmed"


def _price():
    """detect/price.py, or None. Never raises."""
    try:
        d = os.path.join(ROOT, "detect")
        if d not in sys.path:
            sys.path.insert(0, d)
        import price
        return price
    except Exception:
        return None


def _since_word(first_ts):
    """'13:20' when it started in the last day, the full date when it is older."""
    s = str(first_ts or "")
    if len(s) < 16:
        return s or "it was first seen"
    try:
        t = dt.datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=dt.UTC)
        if (dt.datetime.now(dt.UTC) - t).total_seconds() <= 24 * 3600:
            return t.strftime("%H:%M")
    except Exception:
        pass
    return s[:16]


def money_lines(f):
    """[money line, hedge line], or [] when there is nothing honest to say.

    No moca_since means this finding's value is a share or a count, not an
    amount — concentration ratios, first-time-recipient counts. Omit the line
    rather than turn a ratio into a dollar figure."""
    try:
        total = float(f.get("moca_since"))
    except (TypeError, ValueError):
        return []
    if total <= 0:
        return []
    p = _price()
    usd_total, note = p.usd(total) if p else (None, "price unavailable")
    dollars = p.fmt_usd(usd_total) if p else None
    try:
        rate = float(f.get("rate_per_h") or 0)
    except (TypeError, ValueError):
        rate = 0.0

    line = f"~{total:,.0f} MOCA"
    if dollars:
        line += f" (~{dollars})"
    line += f" since {_since_word(f.get('first_ts'))}"
    if rate > 0:
        # Same units on both halves of the line. A total in MOCA-and-dollars beside a
        # rate in dollars only reads as two different quantities.
        rate_usd = p.fmt_usd(p.usd(rate)[0]) if p else None
        line += f", about {rate:,.0f} MOCA/h" + (f" (~{rate_usd}/h)" if rate_usd else "")
    return [f"<b>Money</b>  {line}",
            f"<i>Treasury payouts to this wallet only \u00b7 {HEDGE} \u00b7 {note}</i>"]


# ------------------------------------------------------------------ who can pause

#: The four fields detect/thresholds.json:kill_switch must all carry before any
#: message may claim that somebody can stop a payout. Half a block is not an owner.
_KS_REQUIRED = ("owner", "contact", "commitment_min", "agreed_ts")

#: Words that still mean "nobody". `escalation_owner` reads "Po (interim)" today and
#: renders as a name, which is the worse failure: the reader sees the question
#: answered. A stand-in typed into kill_switch.owner must not silence the UNASSIGNED
#: block either, so the placeholder test lives in code, not in a convention.
_PLACEHOLDER = re.compile(r"unassigned|interim|\btbd\b|\btba\b|\bnobody\b|\bnone\b|\?\?", re.I)

#: What notify/telegram.py prints when it cannot import this module at all.
PAUSE_UNASSIGNED_TERSE = "who can pause: UNASSIGNED — nobody has agreed to stop a payout"


def _thresholds():
    """detect/thresholds.json with the THRESHOLDS_JSON runtime override applied.

    The override matters more here than anywhere else in the file: the kill switch is
    a real person's name and handle, and detect/thresholds.json is in the PUBLIC repo.
    Naming the one person who can stop the money, world-readably, hands an operator
    somebody to social-engineer. So the committed default stays null — which is also
    what CI therefore tests — and the live name can arrive in the secret instead.

    Never silent: a page must not quietly acquire a pause owner, or quietly lose one,
    because a file or a secret failed to parse. Both failures resolve to UNASSIGNED,
    which understates what we can do — the recoverable direction.
    """
    try:
        with open(os.path.join(ROOT, "detect", "thresholds.json")) as fh:
            thr = json.load(fh)
    except Exception as ex:
        print(f"explain: cannot read thresholds.json ({ex!r}) — treating the kill "
              "switch as UNASSIGNED", file=sys.stderr)
        thr = {}
    env = os.environ.get("THRESHOLDS_JSON")
    if env:
        try:
            thr.update(json.loads(env))
        except Exception as ex:
            print(f"explain: THRESHOLDS_JSON override IGNORED, it does not parse "
                  f"({type(ex).__name__}); the committed defaults are live", file=sys.stderr)
    return thr


def kill_switch():
    """(name, contact, minutes) of the platform person who can pause a creator's
    rewards, or None when there is not one yet.

    Read at send time rather than stamped on the finding: who holds this authority is
    a fact about the organisation on the day the message goes out, so a finding first
    seen before the name existed would otherwise print UNASSIGNED for the rest of its
    life. It also adds no bytes to a state file already capped at 900,000 B.
    """
    ks = _thresholds().get("kill_switch") or {}
    if any(not ks.get(k) for k in _KS_REQUIRED):
        return None
    if _PLACEHOLDER.search(str(ks.get("owner"))):
        return None
    mins = _int(ks.get("commitment_min"))
    if not mins:
        # A name with no readable number of minutes is not a commitment. Failing
        # closed here prints UNASSIGNED, which is true; failing open would print a
        # name beside a promise nobody made.
        return None
    return (str(ks["owner"]), str(ks["contact"]), mins)


def _bot_contact():
    """thresholds.escalation_owner — who runs this bot. Not who can pause anything."""
    return _thresholds().get("escalation_owner") or "nobody named"


def pause_lines(f):
    """The kill-switch block for a message, as lines. Never empty.

    Every page ends by asking for a pause, and until now it then printed
    "Who to ask  Po (interim)" — the alert telling Po to ask Po (council §5, vote 8).
    On 20 August 2.4M of the 2.66M MOCA was paid out AFTER the first alert fired
    because nobody held this authority, so an unheld kill switch is printed as unheld,
    loudly, and the bot's own contact is labelled as what it is.

    This says who a person should go and talk to. Nothing here pauses anything: the
    bot informs and never acts (council §6.1).
    """
    ks = kill_switch()
    if ks:
        name, contact, mins = ks
        within = f", agreed to pause within {mins} minutes" if mins else ""
        # An imperative, not a fact. The line this replaced said "Who to ask", and a
        # bare name reads as provenance — somebody at 03:00 has to be told to act.
        return ["", f"<b>Who can pause</b>  Ask {_esc(name)} — {_esc(contact)}{within}"]
    who = _esc(_bot_contact())
    if f.get("tier") == "page":
        return ["", "⛔ <b>Who can pause  UNASSIGNED</b>",
                "<i>No platform person has agreed to stop a creator's rewards, and there is no "
                "agreed response time. Nothing in this channel can pause anything — a person "
                "has to ask by hand and find whoever is awake. On 20 August 2.4M of the 2.66M "
                "MOCA was paid out after the first alert fired, for this reason.</i>",
                f"<i>{who} runs this bot: they can raise it and can add you to the on-call list, "
                "but they cannot stop a payout.</i>"]
    return ["", "<b>Who can pause</b>  ⛔ UNASSIGNED — nobody has agreed to stop a payout; "
                f"{who} runs this bot and cannot stop one either"]


def pause_terse(f):
    """One line of the same fact, for notify/telegram.py's fallback renderer."""
    ks = kill_switch()
    if ks:
        name, contact, mins = ks
        return (f"who can pause: {_esc(name)} — {_esc(contact)}"
                + (f" (within {mins} min)" if mins else ""))
    return PAUSE_UNASSIGNED_TERSE


# ------------------------------------------------------------------ the message

def humanise(f):
    """Turn a finding dict into an HTML message for whoever is awake."""
    sig = str(f.get("signal", "")).lstrip("#")
    spec = SIGNALS.get(sig, FALLBACK)
    icon, urgency = TIER_WORD.get(f.get("tier", "notify"), TIER_WORD["notify"])

    meas = measured(f)
    money = money_lines(f)
    # The money block carries the hedge already. Printing it again as an orphan
    # paragraph three lines above teaches the reader to skip both.
    inferred = None if money else _inferred_note(f, meas)
    what = spec["what"]
    try:
        what = what.format(value=meas, count=f.get("value"),
                           window=_window(f), threshold=f.get("threshold", "the usual level"))
    except (KeyError, IndexError):
        pass

    esc = f.get("escalation") or ""
    if esc == "activity after containment":
        lines = ["🛡🚨 <b>This is still happening after the fix was applied</b>",
                 "<i>Needs attention now — the containment did not hold</i>", "",
                 f"You marked this contained{(' (' + _esc(f['status_note']) + ')') if f.get('status_note') else ''}, "
                 f"but the same wallet is active again. {_grew_words(f)}", "",
                 f"<b>Measured</b>  {meas}" + (f"  {_normal_for(f, spec)}" if _normal_for(f, spec) else "")]
        if inferred:
            lines += ["", inferred]
        if money:
            lines += ["", *money]
        lines += ["", "<b>Why this matters</b>  A fix that does not stop the activity means the money is still "
                      "moving and the assumption behind the fix is wrong.", "",
                  "<b>What to do</b>  Re-check what was actually paused, and whether the operator moved to "
                  "another wallet or another route."]
        if f.get("key", "").startswith("0x"):
            lines += ["", f"Wallet  <code>{f['key']}</code>"]
        lines += pause_lines(f)
        return "\n".join(lines) + "\n\n" + _footer(f)

    if esc == "still growing since you reported it":
        lines = ["📣 <b>Update — the case you reported is still growing</b>",
                 "<i>Worth a look today</i>", "",
                 f"{_grew_words(f, verb='reported it')}", "",
                 f"<b>Measured</b>  {meas}" + (f"  {_normal_for(f, spec)}" if _normal_for(f, spec) else "")]
        if inferred:
            lines += ["", inferred]
        if money:
            lines += ["", *money]
        lines += ["", "<b>Why this matters</b>  The report has not stopped it yet — whoever is acting on it "
                      "may not have applied a change, or the change is not working.", "",
                  "<b>What to do</b>  Chase the action, or mark it contained once a fix is in, so I can tell "
                  "you if it holds."]
        if f.get("key", "").startswith("0x"):
            lines += ["", f"Wallet  <code>{f['key']}</code>"]
        lines += pause_lines(f)
        return "\n".join(lines) + "\n\n" + _footer(f)

    normal = _normal_for(f, spec)
    lines = [f"{icon} <b>{spec['title']}</b>", f"<i>{urgency}</i>", "", what, "",
             f"<b>Measured</b>  {meas}" + (f"  {normal}" if normal else "")]
    if inferred:
        lines += ["", inferred]
    if money:
        lines += ["", *money]
    lines += ["", f"<b>Why this matters</b>  {spec['why']}", "",
              f"<b>What to do</b>  {spec['do']}"]

    if f.get("key", "").startswith("0x"):
        lines += ["", f"Wallet  <code>{f['key']}</code>"]
    elif f.get("key"):
        lines += ["", f"Scope  {f['key']}"]
    lines += pause_lines(f)

    return "\n".join(lines) + "\n\n" + _footer(f)


# Short, self-explaining lines for the silent digest. Each says what the number IS.
DIGEST_LINE = {
    "13":  ("Withdrawals to outside wallets", "{n} Minds sent MOCA off-platform (allowed; recorded for the trail)"),
    "S-G": ("Watch-list wallets moved", "{n} wallets from the August incident moved MOCA"),
    "10i": ("One creator took a large share (our own creators included)", "{n} window(s) where a single creator took most of the equip rewards"),
    "9":   ("Many first-time payout recipients", "{n} hour(s) with an unusual number of brand-new wallets paid"),
    "S-X": ("Balances moved in incident-linked wallets", "{n} wallet(s) changed balance"),
    "INV-10": ("One creator is taking an unusual share of the smaller usage rewards",
               "{n} window(s) where one creator took most of the invoke-sized payouts"),
    "INV-11": ("A creator got a burst of the smaller usage rewards in seconds",
               "{n} burst(s) of invoke-sized payouts inside a minute"),
}


def _digest_block(items, sig):
    """One signal's lines inside the digest."""
    title, tmpl = DIGEST_LINE.get(sig, (SIGNALS.get(sig, FALLBACK)["title"], "{n} event(s)"))
    out = [f"<b>{title}</b>", f"   {tmpl.format(n=len(items))}"]
    biggest = max(items, key=lambda x: len(str(x.get("detail") or "")))
    if biggest.get("detail"):
        out.append(f"   e.g. {measured(biggest)}")
    out.append("")
    return out


def digest(findings):
    """One silent, readable summary instead of a list of codes.

    Findings held in shadow are separated out and named. They DID cross a level
    that would otherwise alert, so the standing line below — "nothing here crossed
    a level that asks for a decision" — would be a false all-clear printed over
    them (§6.3, §6.6). Being a new and unmeasured check is a reason not to wake
    someone; it is never a reason not to say the thing happened.
    """
    import collections
    shadow = [f for f in findings if f.get("shadow_of")]
    plain = [f for f in findings if not f.get("shadow_of")]
    lines = ["📋 <b>For the record</b>", ""]
    if shadow:
        by = collections.OrderedDict()
        for f in shadow:
            by.setdefault(str(f.get("signal", "")).lstrip("#"), []).append(f)
        lines += [SHADOW_HEADING,
                  "<i>These crossed a level that would normally alert. They are new checks "
                  "whose normal range has not been measured on live traffic yet, so they are "
                  "written down instead of waking anyone. That is a decision about the check, "
                  "not a judgement that the activity is harmless — it is not an all-clear.</i>",
                  ""]
        for sig, items in by.items():
            would = str(items[0].get("shadow_of") or "alert")
            lines += _digest_block(items, sig)[:2]
            lines.append(f"   would have been a <b>{would}</b> if this check were live")
            biggest = max(items, key=lambda x: len(str(x.get("detail") or "")))
            if biggest.get("detail"):
                lines.append(f"   e.g. {measured(biggest)}")
            lines.append("")
    if plain:
        groups = collections.OrderedDict()
        for f in plain:
            groups.setdefault(str(f.get("signal", "")).lstrip("#"), []).append(f)
        # "The rest of this list" is only true when a shadow block precedes it. On the
        # ordinary day — nothing in shadow, which is most days — it opened the most-read
        # message in the channel with a clause referring to nothing above it.
        opener = ("The rest of this list crossed no level" if shadow
                  else "Nothing here crossed a level")
        lines += [f"<i>{opener} that asks for a decision today. "
                  "These are counts, not clearances.</i>", ""]
        for sig, items in groups.items():
            lines += _digest_block(items, sig)
    lines.append("<i>/cases shows everything still waiting on a person.</i>")
    return "\n".join(lines)
