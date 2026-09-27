"""One switch for what the live pipeline may say in a PUBLIC log.

This repository is public, so every GitHub Actions log it produces is world-readable.
A log line such as "new=12 new_by_tier={'notify': 12}" or "incident: queue=133" is a
live readout of the detector: it tells whoever is being watched whether they have
been noticed, how much, and when. Standing rule: no public log may reveal monitoring
status (findings, tiers, counts, signal values, shadow signals, which hashes fired,
ack or enrichment state, backlog) and no identities or handles.

    private_print(detail, public=None)

* Inside GitHub Actions (GITHUB_ACTIONS=true) only `public` is printed — a fixed,
  number-free phrase such as "detect: ran" or "notify: failed" — and nothing at all
  when `public` is None.
* Everywhere else (a laptop, a local replay) `detail` is printed in full.

Nothing is routed anywhere else. Logs are not a record: anything a person must know
already reaches them through Telegram or the private state file (e.g. aged-out
findings are announced from `retired_notice`, send failures from `send_failure_alarmed`).
"""
import os
import sys

__all__ = ["private_print", "in_public_ci"]


def in_public_ci():
    """True when stdout lands in a world-readable Actions log."""
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


def private_print(detail, public=None, file=None):
    """Print `detail` locally; inside Actions print only `public` (or nothing)."""
    out = file or sys.stdout
    if in_public_ci():
        if public:
            print(public, file=out, flush=True)
        return
    print(detail, file=out, flush=True)
