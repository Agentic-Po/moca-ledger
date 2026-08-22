#!/usr/bin/env python3
"""Exit-leg balance watch (build plan 1.7) — S-X.

The MOCA ledger only sees MOCA Transfer events; the exit leg (in-wallet swap
to WETH/ETH, then a hop to an exchange) is invisible to it. This module closes
part of that gap with a cheap per-slot balance poll over the small plaintext
operational list in labels/exit_watch.json (public-infrastructure / already-
published addresses only — the salted-hash watchlist cannot be reversed).

Per run, per address: ONE batched JSON-RPC request (eth_blockNumber +
eth_getBalance + 4x ERC-20 balanceOf) against the same endpoint list crawl.py
uses, >= pace_s between addresses, whole run capped at budget_s. Fail-soft: a
failed address keeps its previous reading and increments its `stale` counter.

State: detect/balances.json {address: {eth, moca, weth, usdc, mente, block,
ts, stale}} — committed every slot so movement is diffable in git history.

Findings (same Finding dataclass as the ledger signals, tier notify, signal
"S-X", dedupe key "exit:<address>"):
  - any watched balance DECREASES by more than the configured floor
    (thresholds.json -> balance_watch: drop_moca / drop_eth / drop_weth /
    drop_usdc / drop_mente), or
  - an exchange_deposit address RECEIVES anything above deposit_min.

Never runs in replay.py, tests, or CI: run.py only calls poll() when neither
--dry-run, --as-of, --no-balance nor SKIP_BALANCE_WATCH=1 is set. Standalone:
    python3 detect/balance_watch.py --once [--quiet] [--dry-run]
stdout with --quiet carries counts only, never addresses.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from signals import Finding, ACTION, load_thresholds, utc  # noqa: E402

try:                       # single source of truth for the endpoint list
    from crawl import RPCS
except Exception:          # same defaults as crawl.py if import ever fails
    RPCS = [u for u in (os.environ.get("BASE_RPCS") or
            "https://mainnet.base.org,https://base.publicnode.com,https://base-rpc.publicnode.com,https://1rpc.io/base,https://base.drpc.org").split(",") if u.strip()]

WATCH_FILE = os.path.join(ROOT, "labels", "exit_watch.json")
STATE_FILE = os.path.join(HERE, "balances.json")
SIGNAL = "S-X"

# token symbol -> (contract, decimals); decimals verified on-chain 2026-08-22
TOKENS = [
    ("moca",  "0x2b11834ed1feaed4b4b3a86a6f571315e25a884d", 18),
    ("weth",  "0x4200000000000000000000000000000000000006", 18),
    ("usdc",  "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 6),
    ("mente", "0x4cd9a847f39106e19a4e41aea8a232e915c82af5", 18),
]
BALANCEOF = "0x70a08231"                     # balanceOf(address)
DEFAULTS = {"drop_moca": 1000, "drop_mente": 1000, "drop_eth": 0.05,
            "drop_weth": 0.05, "drop_usdc": 100, "deposit_min": 0.000001,
            "pace_s": 1.0, "budget_s": 90}


def rpc_batch(calls, timeout=20, tries=4):
    """POST one JSON array of calls; rotate endpoints on failure."""
    body = json.dumps(calls).encode()
    last = None
    for attempt in range(tries):
        url = RPCS[attempt % len(RPCS)]
        try:
            req = urllib.request.Request(url, data=body, headers={
                "content-type": "application/json",
                "User-Agent": "Mozilla/5.0 (moca-ledger/1.0; polite crawler)"})
            j = json.load(urllib.request.urlopen(req, timeout=timeout))
            if isinstance(j, list) and len(j) == len(calls) and all("result" in c for c in j):
                return {c["id"]: c["result"] for c in j}
            last = "malformed batch reply"
        except Exception as e:
            last = str(e)[:80]
        time.sleep(0.5)
    raise RuntimeError(f"batch rpc failed: {last}")


def read_address(addr):
    """One batched request -> {eth, moca, weth, usdc, mente, block, ts}."""
    calls = [{"jsonrpc": "2.0", "id": 0, "method": "eth_blockNumber", "params": []},
             {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [addr, "latest"]}]
    for i, (sym, tok, dec) in enumerate(TOKENS):
        data = BALANCEOF + addr.lower().replace("0x", "").rjust(64, "0")
        calls.append({"jsonrpc": "2.0", "id": 2 + i, "method": "eth_call",
                      "params": [{"to": tok, "data": data}, "latest"]})
    res = rpc_batch(calls)
    out = {"eth": int(res[1], 16) / 1e18}
    for i, (sym, tok, dec) in enumerate(TOKENS):
        out[sym] = int(res[2 + i], 16) / 10 ** dec if res[2 + i] not in ("0x", None) else 0.0
    out["block"] = int(res[0], 16)
    out["ts"] = int(time.time())
    return out


def _load_json(path, default):
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            return default
    return default


def _moves(prev, cur, role, T, receipt_alert=True):
    """Yield (kind, token, delta, new_balance) for every reportable change.

    `receipt_alert` comes from the watch entry (default on). It used to read a
    `meta` name that was never bound anywhere in this module: the first receipt
    at an exchange_deposit address would have raised NameError, and run.py would
    have swallowed the whole polling pass as a soft-fail — losing exactly the
    finding this watch exists to produce, at exactly the moment it matters."""
    if not prev:
        return
    for sym in ("eth", "moca", "weth", "usdc", "mente"):
        a, b = float(prev.get(sym, 0.0)), float(cur.get(sym, 0.0))
        d = b - a
        if d <= -float(T.get(f"drop_{sym}", DEFAULTS[f"drop_{sym}"])):
            yield ("drop", sym, d, b)
        elif (d >= float(T.get("deposit_min", DEFAULTS["deposit_min"]))
              and role == "exchange_deposit" and receipt_alert):
            yield ("deposit", sym, d, b)


def poll(root=ROOT, thresholds=None, quiet=False, write=True):
    """One polling pass. Returns a list of Finding objects (tier notify)."""
    thr = thresholds or load_thresholds()
    T = dict(DEFAULTS)
    T.update(thr.get("balance_watch", {}) if isinstance(thr.get("balance_watch"), dict) else {})
    doc = _load_json(WATCH_FILE if root == ROOT else os.path.join(root, "labels", "exit_watch.json"), {})
    entries = [(e["address"].lower(), e.get("role", "sink"), bool(e.get("receipt_alert", True)))
               for e in doc.get("addresses", [])]
    state_path = STATE_FILE if root == ROOT else os.path.join(root, "detect", "balances.json")
    state = _load_json(state_path, {})
    t0 = time.time()
    budget = float(T["budget_s"])
    moved = {}                                 # addr -> list of (kind, token, delta, new)
    ok = skipped = 0
    for i, (addr, role, receipt_alert) in enumerate(entries):
        if time.time() - t0 > budget - 5:      # out of budget: rest keep old readings
            for a2, _, _ra in entries[i:]:
                cur = state.get(a2, {})
                cur["stale"] = int(cur.get("stale", 0)) + 1
                state[a2] = cur
                skipped += 1
            break
        if i:
            time.sleep(float(T["pace_s"]))
        prev = state.get(addr)
        try:
            cur = read_address(addr)
        except Exception:                      # fail-soft: keep previous reading
            cur = dict(prev or {})
            cur["stale"] = int(cur.get("stale", 0)) + 1
            state[addr] = cur
            skipped += 1
            continue
        cur["stale"] = 0
        hits = list(_moves(prev, cur, role, T, receipt_alert))
        if hits:
            moved[addr] = hits
        state[addr] = cur
    state = {a: state[a] for a, _, _ra in entries if a in state}   # prune de-listed
    if write:
        tmp = state_path + ".tmp"
        json.dump(state, open(tmp, "w"), indent=1, sort_keys=True)
        os.replace(tmp, state_path)

    roles = {a: r for a, r, _ra in entries}
    evidence = [["address", "role", "eth", "moca", "weth", "usdc", "mente", "stale"]]
    for a, _r, _ra in entries:
        s = state.get(a, {})
        evidence.append([a[:10], roles.get(a, "?"),
                         f"{s.get('eth', 0):.4f}", f"{s.get('moca', 0):,.1f}",
                         f"{s.get('weth', 0):.4f}", f"{s.get('usdc', 0):,.2f}",
                         f"{s.get('mente', 0):,.1f}", s.get("stale", 0)])

    findings = []
    now = int(time.time())
    for addr, hits in moved.items():
        role = roles.get(addr, "sink")
        # one finding per address (dedupe key exit:<addr>); biggest move headlines
        kind, sym, delta, newbal = max(hits, key=lambda h: abs(h[2]))
        floor = T.get(f"drop_{sym}", DEFAULTS[f"drop_{sym}"])
        head = [f"{sym.upper()} {delta:+,.4f} (balance moved)",
                f"new balance {newbal:,.4f} {sym.upper()}",
                f"role: {role}"]
        detail = "; ".join(f"{s} {d:+,.4f} -> {b:,.4f}" for k, s, d, b in hits)
        f = Finding(signal=SIGNAL, key=f"exit:{addr}", tier="notify",
                    value=round(delta, 6),
                    threshold=(T["deposit_min"] if kind == "deposit" else -float(floor)),
                    window="slot", ts=now, headline=head, detail=detail,
                    recommended_action=ACTION["notify"], evidence=list(evidence))
        d = f.to_state()
        d["episode_first"] = utc(now)
        d["episode_last"] = utc(now)
        d["episode_fires"] = len(hits)
        f._state = d
        findings.append(f)

    if quiet:   # counts only — never addresses (public Actions logs)
        print(f"balance_watch: addresses={len(entries)} ok={len(entries) - skipped} "
              f"stale={skipped} findings={len(findings)} elapsed={time.time() - t0:.0f}s")
    else:
        print(f"balance_watch: {len(entries)} addresses, {skipped} stale, "
              f"{len(findings)} moved · {time.time() - t0:.0f}s")
        for row in evidence[1:]:
            mark = " <- moved" if any(row[0].startswith(a[:10]) for a in moved) else ""
            print("  " + "  ".join(str(c).rjust(12) for c in row) + mark)
    return findings


def main():
    p = argparse.ArgumentParser(description="exit-leg balance watch (S-X)")
    p.add_argument("--once", action="store_true", help="single pass (default)")
    p.add_argument("--quiet", action="store_true", help="counts only on stdout")
    p.add_argument("--dry-run", action="store_true", help="poll but do not write balances.json")
    a = p.parse_args()
    if os.environ.get("SKIP_BALANCE_WATCH"):
        print("balance_watch: skipped (SKIP_BALANCE_WATCH set)")
        return 0
    findings = poll(quiet=a.quiet, write=not a.dry_run)
    if not a.quiet:
        for f in findings:
            print(f"  notify {f.signal} {f.key[:15]}  " + " | ".join(f.headline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
