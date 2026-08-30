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
| `catalog.py` | Measured catalog of every dataset here → `catalog.json` + `DATASETS.md` |
| `tests/test_pii.py` | Gate that fails the build if anything privacy-sensitive enters the tree |
| `tests/test_state.py` | Gate on the detector's memory: size cap, restore fallback, no enrichment re-dispatch |
| `notify/selftest.py` | Daily end-to-end proof that an alert can still reach the channel |
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

## Who can change a case

The Telegram bot **informs; it never acts on the platform**. The only thing a person can
change through it is a case's status (`reported` · `contained` · `watching` · `closed`),
and that is restricted to the numeric Telegram user ids in the `TELEGRAM_ACK_USER_IDS`
repo secret. Authorisation **fails closed**: if the secret is unset or empty, nobody can
change anything. Reading (`/cases`, `/status`) stays open to everyone in the group.

Adding a colleague is one line. Have them send anything to the bot first — the refusal
reply tells them their own numeric user id, which they have no other way to look up. Then
a repo admin runs:

```bash
gh secret set TELEGRAM_ACK_USER_IDS -R Agentic-Po/moca-ledger -b "<existing ids>,<new id>"
```

The value is a comma- or space-separated list and **replaces** the whole list, so include
the ids already there. Only a repo admin can set it — that is the point, and it is why the
refusal message names who to ask rather than just saying no.

## The message ledger

Every message the bot sends and every message a person sends it is written to
`moca-ledger-private:messages/` (`notify/msglog.py`). It exists because a reply that
cannot be matched to a case used to be discarded in silence — a reply typed at 01:32
vanished — and because `by_message` in the state file only ever held alerts and is
pruned to the last 300 entries.

* `index.json` — a bounded map (last 4,000 outbound ids) of message id to case. This
  is what reply matching reads.
* `out-YYYY-MM-NN.jsonl` / `in-YYYY-MM-NN.jsonl` — the append-only archive, rolled by
  month and by size so no single file approaches the Contents API ceiling.
* `pending-links.jsonl` — associations handed over by the private-side tier-2 job,
  which commits with git while this pushes through the API.

Resolution follows the parent chain, so a reply to a chart or to a tier-2 detail lands
on the alert those hang off. When a message genuinely cannot be matched the bot says
what it *was* ("that was the quiet daily digest") rather than guessing, and nothing is
recorded against a case. Two permanent limits: Telegram will not enumerate messages
sent before the ledger existed, and it cannot be asked what an arbitrary id was — for
those, reply to the message and send `/link <case id>`.

## Data catalog

`catalog.py` measures every dataset in this repo — rows, bytes and coverage are computed
off the files on every crawl, never hand-typed — and writes `catalog.json` (machine) and
[`DATASETS.md`](DATASETS.md) (human). Each entry also states its provenance and, more
usefully, what is **not** in it, so a consumer learns the gap from the catalog rather than
from a wrong number. Private datasets are listed by name only: no schema, no size, no
coverage, because a findings file's size is a count of open findings. `python3 catalog.py
--check` recomputes and fails if the committed catalog disagrees with the data.
Agentic-Po/skill-payout-dashboard fetches this `catalog.json` to show both ledgers in one
table; that fetch is best-effort on its side, so neither repo's CI can break the other's.

## Status

`heartbeat.json` carries the last successful run, block lag and detector health.
