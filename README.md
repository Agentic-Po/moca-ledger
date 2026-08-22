# moca-ledger

A complete, continuously-crawled ledger of **MOCA (Base) ERC-20 Transfer events**, plus a
detection floor that watches reward-distribution flows for abuse patterns and alerts a
private channel.

## Why

Reward economies fail quietly: payouts look normal in aggregate while a small number of
recipients take a large share. Aggregate USD-per-hour monitoring misses that. This repo
measures the *shape* of the flow — concentration, bursts, fan-in, velocity — against
organic baselines computed from the same ledger.

## What is here

| Path | Contents |
|---|---|
| `crawl.py` | Resumable crawler: `eth_getLogs` over the MOCA contract, polite pacing, adaptive window |
| `data/YYYY-MM-DD.jsonl` | One row per transfer: `block, ts, tx, li, from, to, value` (raw wei) |
| `detect/` | Detector code, aggregate organic baselines, salted-hash address sets |
| `labels/` | Public infrastructure addresses (treasury, reward source, cognition sink, AMMs) and campaign windows |
| `tests/test_pii.py` | Gate that fails the build if anything privacy-sensitive enters the tree |
| `.github/workflows/` | 10-minute crawl + detect + notify loop, CI, monthly archive |

Timestamps are derived from Base's fixed 2 s block time (verified exact over 2M blocks),
so the crawler needs one RPC call per window and no per-block lookups.

## What is deliberately **not** here

Account-level data (any mapping from wallets to platform accounts, contact details or
network metadata), investigation notes, and live incident records are kept out of this
repository by design. `detect/mindset.json` and the excluded-address list are **salted
hashes**; the salt is an Actions secret. `tests/test_pii.py` enforces this on every push.

## Running it

```bash
python3 crawl.py                # catch up to chain tip (resumable via state.json)
python3 tests/test_pii.py --tree .
```

## Status

`heartbeat.json` carries the last successful run, block lag and detector health.
