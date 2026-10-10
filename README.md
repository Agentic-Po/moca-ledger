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
| `detect/` | Detector code, committed thresholds, aggregate organic baselines |
| `labels/` | Public infrastructure addresses (treasury, reward source, cognition sink, AMMs) and campaign windows |
| `catalog.py` | Measured catalog of every dataset here → `catalog.json` + `DATASETS.md` |
| `tests/test_pii.py` | Gate that fails the build if anything privacy-sensitive enters the tree |
| `tests/test_state.py` | Gate on the detector's memory: size cap, restore fallback, no enrichment re-dispatch |
| `notify/selftest.py` | Daily end-to-end proof that an alert can still reach the channel |
| `.github/workflows/` | 10-minute crawl + detect + notify loop, CI, daily self-test, weekly keep-alive |

Timestamps are derived from Base's fixed 2 s block time (verified exact over 2M blocks),
so the crawler needs one RPC call per window and no per-block lookups.

## What is deliberately **not** here

Account-level data (any mapping from wallets to platform accounts, contact details or
network metadata), investigation notes, and live incident records are kept out of this
repository by design. So is **detector status**: open findings, tiers, fire counts,
shadow signals, which hashes fired, and ack/enrichment state live only in
`moca-ledger-private` (the state file, restored and saved by `notify/state_sync.py`).
The salted-hash oracle the detector reads — class labels (`labels-lite.json`), the
watchlist and the Mind set (`mindset.json`) — is restored from `moca-ledger-private:oracle/`
at run time and is gitignored here; the salt is an Actions secret. Public Actions logs
print number-free status lines only (`notify/privlog.py`, held by
`tests/test_quiet_logs.py`). `tests/test_pii.py` enforces the tree, including handles
and names, on every push.

## Running it

```bash
python3 crawl.py                # catch up to chain tip (resumable via state.json)
python3 tests/test_pii.py --tree .
```

## Who can change a case

Outgoing alerts, photo captions and command replies display contact handles as plain
text, without the mention prefix, to avoid extra Telegram mention notifications.

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

`heartbeat.json` carries crawl run health only: `run_ts`, `crawl_ok`, `rows_total`,
`ledger_last`, `lag_blocks`. Detector health (address-set age, open findings, fires,
shadow list, override errors) is written to `detector_health` in the private state and
read by the bot through `notify/health.py` — the daily proof of life, `/status`, the
watchdog and the weekly check all still see it, in the private group.


## Current reward policy (2026-10-04)

The resumed reward era began on 2026-09-14 at 14:19 UTC. Equip creators receive
$0.05 (half of the $0.10 cost); invoke creators receive $0.005 (half of $0.01).
Contemporary $1 payouts are system top-ups, not skill rewards. Historical $1
and $0.10 rewards retain their original classification. The August reward
pause ends at the restart rather than remaining active indefinitely.

The size classifier keeps both $3 grant-sized and ambiguous $10 credit-sized
transfers outside skill counters. A $10 new-user grant overlaps a purchased
credit pack; size alone never verifies a purchase. The exact grant-policy
cutover is not assumed, so historical grant amounts are not rewritten.

The dollar unit remains an estimate from prior-day transfer samples, with
current-era samples restricted to the system-top-up band; the older $3 grant
and old invoke bands cannot teach today's reward unit. This is size inference,
not a transaction-type oracle. If prices move beyond the sampling bands,
classification can degrade and requires a separate rate-oracle improvement.

Current skill rewards are checked together against $4 (80 equips, 800 invokes,
or a mixture) using integer half-cent units. Notices concern completed UTC
clock hours and explicitly request verification of the platform's cap window.
A wallet total is a lower bound on its user's total; the detector cannot prove
a combined multi-wallet user total without a private owner mapping. No notice
claims that rolling-window or user-level enforcement has been verified.

Legacy skill-volume heuristics remain active for historical incident replay.
In the current cheaper-reward era they contribute digest context until their
baselines are recalibrated; they do not label normal volume a cap failure.
Obsolete current-era reward cases are retained as closed history and have
pending sends cleared once when the new policy first runs. Independent flow,
cash-out and other non-reward detectors remain active.


Wallet notification delivery is handed to the private companion pipeline for fixed read-only verification before publication to the internal Telegram channel. Public runners receive only case-generation decisions and message IDs, never identity or skill-catalogue results. Routine observations are recorded automatically; inactive episodes leave the decision queue and fresh qualifying episodes can reopen automatic archives. Existing platform-health and independent-flow warning paths remain available. Dispatch is batched on the existing workflow; there is no new schedule or local-machine runtime requirement.

Bounded catch-up: the crawler uses a 240-second acquisition budget, including head bootstrap. Only a clean boundary after durable transfer writes and an atomic checkpoint yields exit 75. The existing workflow validates catalog and PII before committing that partial checkpoint; the job remains red and skips detection and healthy-success publication until the finalized target is reached. Private detector memory is saved only after a successful restore and an attempted detector run, never after cancellation. Successful public providers are preferred per method within a process, and a served range ceiling persists after a genuine range refusal; rate limits do not shrink it. No cadence, provider, secret or finality change is included. Catch-up capacity remains dependent on actual successful run frequency and public RPC availability. Socket timeouts bound inactivity rather than guaranteeing a strict wall-clock deadline.

### Saved coverage timestamps in notices

A validated and published catch-up checkpoint notice reports `state.json`'s
`next_block - 1`, using that block's verified RPC header timestamp in SGT and
its age at the explicit notice observation time. It never uses the latest
transfer or the dashboard build clock as a coverage cutoff. Header lookup is
read-only, uses a ten-second request budget and the existing crawler providers;
unavailable or invalid headers show the saved block with time unavailable.
No new schedule, freshness guarantee, detection pass or healthy status is
implied. Transfer timestamps produced by the crawler's block-spacing estimate
remain labelled as estimated event times in the daily heartbeat. Genuine
prepublication failures retain their original failure notice.
