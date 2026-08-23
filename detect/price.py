#!/usr/bin/env python3
"""Daily USD price for MOCA (and MENTE), cached in the repo.

Why a cache instead of a live call: the crawl runs every ten minutes, and a
price feed that is slow, rate-limited or simply down must never break a
detector run and must never make the money line vanish in the middle of an
incident. So the rule is:

  * fetch at most once a day (DeFiLlama, one HTTP GET, no key, no account),
  * write the answer to detect/price.json, which is committed, so the price
    that produced any past alert is in git history,
  * render every money line from that cache, never from a live call,
  * a cache older than STALE_DAYS is still used — it is marked `price stale`
    in the copy rather than silently dropped or silently trusted,
  * every money line carries the AGE of the price it used, not just the date it
    was fetched on. `price cached 2026-08-20` beside a dollar figure on the 23rd
    reads as provenance; `70 h old` reads as what it is.

Nothing here raises. Every entry point returns a value plus a human-readable
note saying how good that value is; callers put the note in the message.

CLI:
    python3 detect/price.py            # refresh if the cache is over 24 h old
    python3 detect/price.py --force    # refresh now
    python3 detect/price.py --show     # print the cache, never touch the network
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "price.json")

# DeFiLlama coin ids. Both contracts are public token contracts and are listed
# in labels/public_addresses.json, so they may appear in a tracked file.
COINS = {
    "MOCA":  "base:0x2b11834ed1feaed4b4b3a86a6f571315e25a884d",
    "MENTE": "base:0x4cd9a847f39106e19a4e41aea8a232e915c82af5",
}
ENDPOINT = "https://coins.llama.fi/prices/current/" + ",".join(COINS.values())

MAX_AGE_H = 24        # refresh no more than once a day
# refresh() is fail-soft by design, so a frozen cache is the EXPECTED failure, not
# the exotic one. At 3 days a price could be 71 h out of date and still print as
# though it were merely "cached"; one missed day is the honest line.
STALE_DAYS = 1        # older than this and the copy says "price stale"
MIN_CONFIDENCE = 0.5  # below this we keep the previous cache rather than overwrite


def _now():
    return dt.datetime.now(dt.UTC)


def load():
    """The cache as written, or {} if it is missing or unreadable."""
    try:
        with open(CACHE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _fetched_at(doc):
    try:
        return dt.datetime.fromisoformat(str(doc.get("fetched_at", "")).replace("Z", "+00:00"))
    except Exception:
        return None


def age_h(doc=None):
    """Hours since the cache was fetched, or None when there is no usable cache."""
    at = _fetched_at(doc if doc is not None else load())
    if at is None:
        return None
    return max(0.0, (_now() - at).total_seconds() / 3600.0)


def fetch(timeout=15):
    """One GET. Returns {symbol: {price, confidence}} or raises."""
    req = urllib.request.Request(ENDPOINT, headers={"User-Agent": "moca-ledger/1.0 (price cache)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        doc = json.load(r)
    coins = doc.get("coins") or {}
    out = {}
    for sym, cid in COINS.items():
        c = coins.get(cid) or {}
        p = c.get("price")
        if isinstance(p, (int, float)) and p > 0:
            out[sym] = {"price": float(p), "confidence": float(c.get("confidence") or 0.0)}
    if not out:
        raise ValueError("no usable price in response")
    return out


def refresh(max_age_h=MAX_AGE_H, force=False, timeout=15):
    """Update detect/price.json if the cache is older than max_age_h.

    Returns (changed: bool, note: str). Never raises: a failed fetch leaves the
    previous cache in place and the note says why, so the caller can print it.
    """
    doc = load()
    a = age_h(doc)
    if not force and a is not None and a < max_age_h:
        return False, f"price: cache is {a:.1f} h old, no fetch needed"
    try:
        got = fetch(timeout=timeout)
    except Exception as e:
        return False, f"price: fetch failed ({type(e).__name__}: {str(e)[:60]}) — keeping cached value"
    low = [s for s, v in got.items() if v["confidence"] < MIN_CONFIDENCE]
    if low and doc.get("prices"):
        return False, f"price: low confidence for {','.join(sorted(low))} — keeping cached value"
    now = _now()
    new = {
        "note": "USD prices from DeFiLlama, fetched at most once a day and committed so past "
                "alerts can be re-priced exactly as they were sent. Read by detect/run.py and "
                "notify/explain.py; a stale cache is marked in the copy, never silently trusted.",
        "source": "https://coins.llama.fi/prices/current/",
        "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": now.strftime("%Y-%m-%d"),
        "prices": {s: round(v["price"], 8) for s, v in sorted(got.items())},
        "confidence": {s: round(v["confidence"], 3) for s, v in sorted(got.items())},
        "coins": COINS,
        "stale_after_days": STALE_DAYS,
    }
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(new, fh, indent=1)
    os.replace(tmp, CACHE)
    return True, "price: refreshed " + " ".join(f"{s}=${v}" for s, v in new["prices"].items())


def age_words(a):
    """'2 h old' / '3 d old'. None when there is no usable timestamp."""
    if a is None:
        return None
    if a < 1:
        return "under 1 h old"
    return f"{a:.0f} h old" if a < 48 else f"{a / 24:.0f} d old"


def price_of(symbol="MOCA"):
    """(price_usd_or_None, note). note is exactly the marker the copy must carry.

    The note always states the age. A reader scanning `~38,141 MOCA (~$321) · price
    cached 2026-08-20` on the 23rd reads the date as where the number came from, not
    as how old the dollars are."""
    doc = load()
    p = (doc.get("prices") or {}).get(symbol)
    if not isinstance(p, (int, float)) or p <= 0:
        return None, "price unavailable"
    a = age_h(doc)
    when = doc.get("date") or str(doc.get("fetched_at"))[:10]
    old = age_words(a)
    if a is None:
        return float(p), "price stale — age unknown"
    if a > STALE_DAYS * 24:
        return float(p), f"price stale — cached {when}, {old}"
    return float(p), f"price cached {when} — {old}"


def usd(amount, symbol="MOCA"):
    """Convert a token amount to USD from the cache.

    Returns (usd_or_None, note). None means we have no price we are willing to
    show — the caller must then omit the dollar figure rather than invent one.
    """
    p, note = price_of(symbol)
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None, note
    if p is None:
        return None, note
    return amount * p, note


def fmt_usd(v):
    """$ figure at a sensible precision, or None."""
    if v is None:
        return None
    av = abs(v)
    if av >= 10:
        return f"${v:,.0f}"
    if av >= 0.01:
        return f"${v:,.2f}"
    return f"${v:,.4f}"


def main():
    ap = argparse.ArgumentParser(description="daily USD price cache for the money lines")
    ap.add_argument("--force", action="store_true", help="fetch even if the cache is fresh")
    ap.add_argument("--show", action="store_true", help="print the cache; never touch the network")
    a = ap.parse_args()
    if not a.show:
        changed, note = refresh(force=a.force)
        print(note)
    doc = load()
    p, note = price_of("MOCA")
    print(f"cache: {doc.get('fetched_at', 'none')} · MOCA={p} · {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
