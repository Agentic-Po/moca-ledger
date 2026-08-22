#!/usr/bin/env python3
"""Plain-English layer for alerts.

An alert is read by whoever is awake, not by whoever built the detector. Every
message therefore opens with a sentence in ordinary Minds vocabulary (Skill,
equip, creator, Mind, reward), says why it matters in money or trust terms, and
says what to do. Signal codes and thresholds go in a footer for the record.

Vocabulary rules kept deliberately: a builder publishes a **Skill**; a user
**equips** then **invokes** it; the Treasury pays the creator ~$1 per equip and
~$0.10 per invoke; a **Mind** is a user's agent and each Mind has a wallet.
"""

# title · what happened (format string) · why it matters · what to do
SIGNALS = {
    "10": {
        "title": "One creator is taking an unusual share of reward payouts",
        "what": "One creator wallet is receiving far more of the equip rewards than any single creator normally does.",
        "why": "Every equip pays about $1 from the Treasury. When one creator takes most of them, it usually means their Skills are being equipped by accounts they control, not by real users.",
        "do": "Look at who is equipping this creator's Skills. If the equippers are brand-new Minds with no other activity, ask for this creator's rewards to be paused.",
    },
    "11": {
        "title": "A creator got a burst of reward payouts in seconds",
        "what": "A single creator wallet received ten or more equip rewards within one minute.",
        "why": "A person cannot equip ten Skills in a minute. This is automated equipping — the fastest form of reward farming, and the clearest sign money is leaving the Treasury right now.",
        "do": "Treat as live. Ask for this creator's rewards to be paused, then review the equipping Minds.",
    },
    "15": {
        "title": "The payout system is running at full capacity",
        "what": "The Treasury is sending reward payouts at the maximum rate its payout system allows.",
        "why": "The payout worker only saturates when something is requesting rewards far faster than real users generate them. On 21 Aug this state coincided with the largest hour of loss.",
        "do": "Stop first, investigate second. Ask for equip and invoke rewards to be paused platform-wide until the cause is known.",
    },
    "4b": {
        "title": "Many Minds are sending their MOCA into one wallet",
        "what": "One wallet is collecting MOCA sent in by many different Minds.",
        "why": "Ordinary users do not pay each other. A collection point is how farmed rewards are gathered before being sold, so this usually names the wallet worth freezing.",
        "do": "Check whether the collecting wallet belongs to a real builder. If not, note the address — it is the one to give an exchange if funds move on.",
    },
    "4a": {
        "title": "Minds are transferring MOCA to each other unusually often",
        "what": "Minds are sending MOCA to each other far more often than usual.",
        "why": "There is no product reason for Minds to pay each other. A spike normally means one operator is shuffling rewards between wallets they own.",
        "do": "No action on its own — watch for a collection wallet to appear next.",
    },
    "S-C": {
        "title": "Many Minds are sending MOCA to an outside wallet",
        "what": "An address that is not a Mind is receiving MOCA from several different Minds.",
        "why": "This is the same consolidation pattern as above, but the destination sits outside the platform entirely — usually one step before a sale.",
        "do": "Note the address. If it starts moving funds to an exchange, it belongs in a freeze request.",
    },
    "S-A": {
        "title": "A creator is earning steadily above what real usage produces",
        "what": "One creator is earning equip rewards steadily, above the level real usage normally produces.",
        "why": "Slower than a burst and easy to miss, but the same pattern: sustained earning without matching real usage. This is what a patient operator looks like.",
        "do": "Compare this creator's equips against actual Skill usage. A popular new Skill can look like this — check before acting.",
    },
    "S-G": {
        "title": "A wallet we are already watching just moved",
        "what": "A wallet connected to the August incident has moved MOCA again.",
        "why": "This wallet is on the watch list from the August incident. Movement means the operator is active again.",
        "do": "Read the tier-2 message that follows for who it is, then decide if it needs escalating.",
    },
    "S-Q": {
        "title": "Quest rewards are being claimed unusually fast",
        "what": "Quest rewards are going out much faster than usual, mostly to Minds created in the last hour.",
        "why": "Quest rewards are meant for new users completing onboarding. A surge of brand-new Minds claiming them is how a farm builds its inventory before using it.",
        "do": "Check whether the new accounts look genuine — same email domains, same IP, created minutes apart are all warning signs.",
    },
    "S-Q2": {
        "title": "Quest rewards are being forwarded straight out",
        "what": "Minds are forwarding their quest reward to another wallet within a day of receiving it.",
        "why": "A genuine new user spends their quest reward on the platform. Immediately forwarding it means the account exists only to collect.",
        "do": "Note where the funds are going — the destination is usually the operator's collection wallet.",
    },
    "S-F": {
        "title": "A reward payout happened while rewards are supposed to be paused",
        "what": "A payout the size of an equip or invoke reward went out after rewards were paused.",
        "why": "Equip and invoke rewards were paused on 21 Aug. A payout in that shape means either the pause has a gap, or this is a different kind of payment that looks similar on-chain.",
        "do": "Ask the platform team to confirm what this payment actually was before assuming the pause failed.",
    },
    "S-X": {
        "title": "Money moved in a wallet linked to the August incident",
        "what": "The balance changed in a wallet that handled proceeds from the August farming.",
        "why": "These wallets held or moved the proceeds of the August farming. Movement now means funds are being shifted, possibly toward an exchange.",
        "do": "If the destination is an exchange deposit address, that is the moment a freeze request has the best chance of working.",
    },
    "EV": {
        "title": "Platform-wide equip rewards are far above normal",
        "what": "Equip rewards across the whole platform are far above their normal daily level.",
        "why": "This catches farming that is spread across many creators so that no single one looks unusual — the total still gives it away.",
        "do": "Check whether the growth is spread across many genuine creators or concentrated in a handful of new ones.",
    },
    "9": {
        "title": "Unusually many wallets are receiving Treasury payouts for the first time",
        "what": "An unusual number of wallets received a Treasury payout for the very first time.",
        "why": "New recipients arriving in bulk usually means new accounts were created in bulk.",
        "do": "Normal during a campaign. If there is no campaign running, look at how those accounts were created.",
    },
    "13": {
        "title": "A Mind moved MOCA out to an outside wallet",
        "what": "A Mind sent MOCA to an address that is not part of the platform.",
        "why": "Withdrawing is a normal, allowed feature — this is recorded for the trail, not because it is wrong on its own.",
        "do": "Nothing, unless this wallet also appears in another alert.",
    },
    "7":  {"title": "A creator's Skills are equipped but never used",
           "what": "This creator's Skills are being equipped but almost never actually used.",
           "why": "Equips earn the creator ~$1 each; invokes are what real usage looks like. Equips without invokes suggest the equipping was the point.",
           "do": "Nothing on its own — useful context when judging another alert."},
    "14": {"title": "Treasury outflow is above its usual level",
           "what": "The Treasury paid out more than its usual amount for this time of day.",
           "why": "Context only — this number alone was the alert that failed to catch the August incident.",
           "do": "Nothing. Read it alongside the other alerts."},
    "composite": {
        "title": "Several warning signs are pointing at the same wallets",
        "what": "More than one alert has fired on connected wallets within a few hours.",
        "why": "Individually each sign can be innocent. Together, on connected wallets and within a few hours, they are the shape the August incident had.",
        "do": "Treat this like a page: read the tier-2 detail, then decide whether to ask for a pause.",
    },
}

FALLBACK = {
    "title": "Unusual activity in the reward flow",
    "what": "{value}",
    "why": "This pattern is outside what normal platform activity produces.",
    "do": "Read the detail below and the tier-2 message that follows.",
}

TIER_WORD = {
    "page":   ("🚨", "Needs attention now"),
    "notify": ("🟠", "Worth a look today"),
    "digest": ("📋", "For the record"),
}


def humanise(f):
    """Turn a finding dict into (headline, body_lines, footer)."""
    sig = str(f.get("signal", "")).lstrip("#")
    spec = SIGNALS.get(sig, FALLBACK)
    icon, urgency = TIER_WORD.get(f.get("tier", "notify"), TIER_WORD["notify"])

    # Prefer the detector's own headline sentence; fall back to value vs threshold.
    head = f.get("headline") or []
    detail = f.get("detail") or (head[0] if head else "")
    value = detail or f"{f.get('value')} (limit {f.get('threshold')})"
    what = spec["what"]
    try:
        what = what.format(value=value, window=f.get("window", "the window"),
                           threshold=f.get("threshold", "the usual level"))
    except (KeyError, IndexError):
        pass

    measured = detail or f"{f.get('value')}"
    limit = f.get("threshold")
    lines = [f"{icon} <b>{spec['title']}</b>", f"<i>{urgency}</i>", "", what, "",
             f"<b>Measured</b>  {measured}" + (f"  (the level that triggers a look: {limit})" if limit not in (None, "") else ""),
             "", f"<b>Why this matters</b>  {spec['why']}", "",
             f"<b>What to do</b>  {spec['do']}"]

    if f.get("key", "").startswith("0x"):
        lines += ["", f"Wallet  <code>{f['key']}</code>"]
    elif f.get("key"):
        lines += ["", f"Scope  {f['key']}"]
    for extra in head[1:3]:
        lines.append(f"• {extra}")
    if f.get("owner"):
        lines += ["", f"Who to ask  {f['owner']}"]

    footer = (f"<i>signal {f.get('signal')} · {f.get('value')} vs {f.get('threshold')} · "
              f"block {f.get('as_of_block','?')}</i>\n<code>/ack {f.get('id','')}</code>")
    return "\n".join(lines) + "\n\n" + footer


# Short, self-explaining lines for the silent digest. Each says what the number IS.
DIGEST_LINE = {
    "13":  ("Withdrawals to outside wallets", "{n} Minds sent MOCA off-platform (allowed; recorded for the trail)"),
    "S-G": ("Watch-list wallets moved", "{n} wallets from the August incident moved MOCA"),
    "10i": ("One creator took a large share (our own creators included)", "{n} window(s) where a single creator took most of the equip rewards"),
    "9":   ("Many first-time payout recipients", "{n} hour(s) with an unusual number of brand-new wallets paid"),
    "7":   ("Skills equipped but not used", "{n} creator(s) earning equips with almost no invokes"),
    "14":  ("Treasury outflow above its usual level", "{n} window(s) above the normal range"),
    "S-X": ("Balances moved in incident-linked wallets", "{n} wallet(s) changed balance"),
}


def digest(findings):
    """One silent, readable summary instead of a list of codes."""
    import collections
    groups = collections.OrderedDict()
    for f in findings:
        groups.setdefault(str(f.get("signal", "")).lstrip("#"), []).append(f)
    lines = ["📋 <b>For the record</b>",
             "<i>Nothing here needs action — this is the background activity log.</i>", ""]
    for sig, items in groups.items():
        title, tmpl = DIGEST_LINE.get(sig, (SIGNALS.get(sig, FALLBACK)["title"], "{n} event(s)"))
        lines.append(f"<b>{title}</b>")
        lines.append(f"   {tmpl.format(n=len(items))}")
        biggest = max(items, key=lambda x: len(str(x.get("detail") or "")))
        if biggest.get("detail"):
            lines.append(f"   e.g. {biggest['detail']}")
        lines.append("")
    lines.append("<i>Ask any alert for detail with /status</i>")
    return "\n".join(lines)
