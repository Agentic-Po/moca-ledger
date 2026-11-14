# Base timestamp correction — 14 November 2026

Base Denim changed canonical blocks from two seconds to 200 milliseconds on 5 November. The crawler continued projecting timestamps with the old cadence from each current head. This changed timestamps and daily partition placement, not the underlying Transfer fields.

The repair uses the qualified first Denim block 53556127 at Unix second 1793901601; subsequent timestamps advance by one whole second every five blocks. Legacy blocks retain their two-second cadence. Both existing Base providers independently agreed on the boundary and sampled historical/current headers. Incompatible future head timing fails before any append.

The offline replay corrected 65,588 timestamps and moved 6,314 rows between daily partitions in the 895,323-event snapshot. Every non-timestamp field and event multiplicity was preserved by count and an order-independent SHA-256 field fingerprint. No cursor, transaction amount, address, transaction hash, or log index was changed. Historical git revisions remain available; no history rewrite or deletion is involved.

Run `python3 tools/repair_denim_timestamps.py` to audit, or add `--apply` to rebuild dates. Rebuild the catalog afterwards. The repair is idempotent. Downstream daily aggregates must use the corrected dates and retain their own reconciliation checks.

Source: [Base Denim specification](https://docs.base.org/upgrades/denim/200ms-blocks).
