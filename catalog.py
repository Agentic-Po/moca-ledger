#!/usr/bin/env python3
"""Computed catalog of every dataset this repo publishes.

Twin of catalog.py in Agentic-Po/skill-payout-dashboard, which fetches this
repo's catalog.json to aggregate both ledgers in one table. Same contract:
rows, bytes and coverage are MEASURED off the files on every build, never
hand-typed, so the published table cannot drift from the data.

Nothing here reads a row's contents beyond its timestamp — the PII gate
(tests/test_pii.py) treats a bare address in a tracked file as operational
intelligence, and a catalog is a tracked file.

  python3 catalog.py           rebuild catalog.json + DATASETS.md
  python3 catalog.py --check   recompute and exit 1 on disagreement

--check tolerance: generated_iso is build time and is never compared; on
datasets marked live the fields the CURRENT month legitimately grows (rows,
max_row_ts, coverage.to) are compared as recomputed >= committed, and bytes
is not compared at all. rows_closed — rows in months strictly before the
current UTC month — is always exact, so a closed day cannot change unnoticed.

MONTH ROLLOVER: an out-of-band --check that spans a UTC month boundary
(catalog committed before the 1st, recomputed after) fails on rows_closed by
design — last month's rows have just become closed. That is the check working,
not data corruption; rebuild the catalog.
"""
import json, os, subprocess, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(HERE, "catalog.json")
DATASETS_MD = os.path.join(HERE, "DATASETS.md")

KINDS = ["ledger", "oracle", "derived", "archive"]


def _size(rel):
    """Private files inside a public directory are excluded — their size is
    itself a signal (see PRIVATE_NAMES)."""
    p = os.path.join(HERE, rel)
    if os.path.isdir(p):
        return sum(os.path.getsize(os.path.join(p, f)) for f in os.listdir(p)
                   if os.path.isfile(os.path.join(p, f)) and f not in PRIVATE_NAMES)
    return os.path.getsize(p) if os.path.exists(p) else 0


def _measured(stamps):
    cur = datetime.now(timezone.utc).strftime("%Y-%m")
    if not stamps:
        return 0, 0, None, None
    return (len(stamps), sum(1 for s in stamps if s[:7] < cur), min(stamps), max(stamps))


def _iso(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _ledger_stamps():
    """One stamp per transfer row. Only `ts` is read — never an address."""
    d = os.path.join(HERE, "data")
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".jsonl"):
            continue
        with open(os.path.join(d, fn)) as fh:
            for line in fh:
                if line.strip():
                    out.append(_iso(json.loads(line)["ts"]))
    return out


def _shallow():
    """A shallow clone (crawl.yml uses fetch-depth: 1) reports the grafted
    boundary commit as the last commit that touched EVERY path, so `git log`
    there dates each label file to the checkout's HEAD — the same
    not-a-measurement failure mtime had. Detect it and decline to measure."""
    try:
        out = subprocess.run(["git", "-C", HERE, "rev-parse", "--is-shallow-repository"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return True
    return out.stdout.strip() != "false"


def _git_stamp(rel):
    """Last COMMIT time of a file, as ISO-8601 without the offset.

    NOT the filesystem mtime: in a fresh clone — i.e. on every CI runner —
    mtime is the checkout time, so `catalog.py --check` failed instantly and
    each crawl rewrote the labels coverage to the runner's clock. That made
    the one number in the table that claims to be "measured off the files"
    a checkout artifact. Commit time is reproducible in any clone of the same
    history. Returns None if git is unavailable or the file is uncommitted
    (a shallow clone with no history for the path, a working-tree-only file),
    in which case the caller drops the stamp rather than inventing one.
    """
    if _shallow():
        return None
    try:
        out = subprocess.run(["git", "-C", HERE, "log", "-1", "--format=%cI", "--", rel],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    v = out.stdout.strip()
    return v[:19] if out.returncode == 0 and v else None


def _label_stamps():
    """Labels are a reference set, not a time series: one stamp per entry,
    dated by the file's last COMMIT so coverage means "as of" and is
    identical in every clone."""
    d = os.path.join(HERE, "labels")
    out = []
    for fn in sorted(os.listdir(d)):
        p = os.path.join(d, fn)
        if not fn.endswith(".json") or not os.path.isfile(p):
            continue
        if fn in PRIVATE_NAMES:
            continue
        try:
            obj = json.load(open(p))
        except json.JSONDecodeError:
            continue
        n = sum(len(v) if isinstance(v, (list, dict)) else 1 for v in obj.values()) \
            if isinstance(obj, dict) else len(obj)
        out.append((max(n, 1), _git_stamp(os.path.join("labels", fn))))
    # An uncommitted (or shallow-clone-invisible) label file still COUNTS —
    # only its stamp is unknown, and it inherits the newest known one so the
    # row count never silently drops.
    known = [st for _, st in out if st]
    fill = max(known) if known else None
    return [st or fill for n, st in out for _ in range(n) if (st or fill)]


def _heartbeat_stamps():
    hb = json.load(open(os.path.join(HERE, "heartbeat.json")))
    return [(hb.get("run_ts") or "")[:19]]


PRIVATE_NAMES = {"exit_watch.json"}

PUBLIC = [
    dict(name="ledger", path=["data/"], kind="ledger", live=True, measure=_ledger_stamps,
         row_schema="block, ts (unix), tx, li (log index), from, to, value (raw wei string)",
         update_cadence="every crawl (10-minute cron)", expected_cadence_minutes=10,
         provenance="eth_getLogs over the MOCA contract on Base, adaptive window, resumable via state.json; timestamps derived from Base's fixed 2s block time (verified exact over 2M blocks)",
         not_included="MOCA Transfer events only — no other token, no ETH, no internal calls, and no USD pricing (this repo never prices a row)"),
    dict(name="labels", path=["labels/"], kind="oracle", live=False, measure=_label_stamps,
         row_schema="public_addresses.json: infrastructure[], token_contracts{}, event_topics{}; allowlist.json; calendar.json: campaign windows; labels-lite.json",
         update_cadence="hand-maintained; changes ride with a PR", expected_cadence_minutes=None,
         provenance="public infrastructure identified off-chain (treasury, reward source, cognition sink, AMM pools) plus campaign windows",
         not_included="no account-level mapping of any kind; the excluded-address list is a salted hash set and exit_watch.json is private (see below)"),
    # snapshot=True: the file is REPLACED every crawl, so coverage.from moves
    # forward legitimately — check() only refuses a backwards move.
    dict(name="heartbeat", path=["heartbeat.json"], kind="derived", live=True, snapshot=True, measure=_heartbeat_stamps,
         row_schema="run_ts, crawl_ok, detect_ok, notify_ok, rows_total, ledger_last, lag_blocks, mindset_age_h, open_findings, fires_last_24h_total",
         update_cadence="rewritten every crawl (10-minute cron)", expected_cadence_minutes=10,
         provenance="written by the crawl-detect-notify workflow at the end of each run",
         not_included="counts and health flags only — no finding detail, no addresses, no thresholds"),
]

PRIVATE = [
    dict(name="alerts/state.json", kind="derived", note="private — see companion doc"),
    dict(name="detect/balances.json", kind="derived", note="private — see companion doc"),
    dict(name="labels/exit_watch.json", kind="oracle", note="private — see companion doc"),
    dict(name="alerts/msglog/", kind="archive", note="private — see companion doc"),
]


def _committed(name, key, default=None):
    """A field from the catalog already in the tree, or default."""
    try:
        for e in json.load(open(CATALOG)):
            if e.get("name") == name:
                return e.get(key, default)
    except (OSError, ValueError):
        pass
    return default


def build_entries():
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for d in PUBLIC:
        rows, closed, lo, hi = _measured([s for s in d["measure"]() if s])
        if d["name"] == "labels" and lo is None:
            # No usable git history for labels/ in this checkout (a shallow
            # clone dates every path to the graft commit). CARRY the
            # committed coverage
            # rather than blanking it or falling back to mtime: mtime is the
            # checkout clock, blanking would churn the catalog on every
            # 10-minute crawl, and carrying keeps --check deterministic in
            # shallow and full clones alike. ci.yml checks out full history,
            # so a real change to labels/ is measured there.
            prev = _committed("labels", "coverage") or {}
            lo, hi = prev.get("from"), prev.get("to")
            rows, closed = _committed("labels", "rows", rows), _committed("labels", "rows_closed", closed)
        out.append({
            "name": d["name"], "path": d["path"], "public": True, "kind": d["kind"],
            "row_schema": d["row_schema"], "rows": rows, "rows_closed": closed,
            "bytes": sum(_size(p) for p in d["path"]),
            "coverage": {"from": lo, "to": hi},
            "generated_iso": gen, "max_row_ts": hi,
            "expected_cadence_minutes": d["expected_cadence_minutes"],
            "update_cadence": d["update_cadence"], "provenance": d["provenance"],
            "not_included": d["not_included"], "live": d["live"],
        })
    for d in PRIVATE:
        # Name only. A row schema for a findings file is a calibration hint,
        # and its SIZE is a count of open findings — so not even bytes.
        out.append({"name": d["name"], "public": False, "kind": d["kind"], "note": d["note"]})
    return out


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def render_md(entries):
    pub = [e for e in entries if e.get("public")]
    priv = [e for e in entries if not e.get("public")]
    L = ["# Data bank", "",
         "Generated by `catalog.py` on every crawl — **do not edit by hand**.",
         "Rows, bytes and coverage are measured off the files, never asserted.", "",
         f"**Data bank: {sum(e['rows'] for e in pub):,} rows · "
         f"{_human(sum(e['bytes'] for e in pub))} across {len(pub)} datasets**", ""]
    for kind in KINDS:
        ks = [e for e in pub if e["kind"] == kind]
        if not ks:
            continue
        L += [f"## {kind}", "", "| Dataset | Path | Rows | Size | Coverage | Cadence |",
              "|---|---|---:|---:|---|---|"]
        for e in ks:
            cov = f"{(e['coverage']['from'] or '?')[:10]} → {(e['coverage']['to'] or '?')[:10]}"
            L.append(f"| `{e['name']}` | {' · '.join('`%s`' % p for p in e['path'])} | "
                     f"{e['rows']:,} | {_human(e['bytes'])} | {cov} | {e['update_cadence']} |")
        L.append("")
        for e in ks:
            L += [f"**`{e['name']}`** — {e['row_schema']}", "",
                  f"- provenance: {e['provenance']}",
                  f"- not included: {e['not_included']}", ""]
    L += ["## private (not published)", "",
          "Listed so the absence is deliberate and visible. No schema, no size,",
          "no coverage: a findings file's size is a count of open findings.", "",
          "| Dataset | Kind | Note |", "|---|---|---|"]
    for e in priv:
        L.append(f"| `{e['name']}` | {e['kind']} | {e['note']} |")
    L.append("")
    return "\n".join(L) + "\n"


def build():
    entries = build_entries()
    json.dump(entries, open(CATALOG, "w"), indent=1)
    open(DATASETS_MD, "w").write(render_md(entries))
    pub = [e for e in entries if e.get("public")]
    print(f"catalog: {len(pub)} public datasets, {sum(e['rows'] for e in pub):,} rows")
    return entries


DRIFTY = ("rows", "max_row_ts")
SKIP_WHEN_LIVE = ("bytes",)


def check():
    if not os.path.exists(CATALOG):
        print("FAIL: catalog.json missing — run catalog.py first")
        return 1
    old, new = json.load(open(CATALOG)), build_entries()
    bad = []
    if len(old) != len(new):
        bad.append(f"entry count {len(old)} -> {len(new)}")
    for o, n in zip(old, new):
        if o.get("name") != n.get("name"):
            bad.append(f"order/name drift: {o.get('name')} vs {n.get('name')}")
            continue
        live = n.get("live", False)
        for k in set(o) | set(n):
            if k == "generated_iso" or (live and k in SKIP_WHEN_LIVE):
                continue
            ov, nv = o.get(k), n.get(k)
            if live and k in DRIFTY:
                if ov is None or nv is None:
                    bad.append(f"{n['name']}.{k}: new/absent field ({ov!r} -> {nv!r}) "
                               f"— rebuild catalog.json after a schema change")
                elif nv < ov:
                    bad.append(f"{n['name']}.{k}: {ov} -> {nv} (went backwards)")
            elif live and k == "coverage":
                if not n.get("snapshot") and (ov or {}).get("from") != (nv or {}).get("from"):
                    bad.append(f"{n['name']}.coverage.from: {ov} -> {nv}")
                if (nv or {}).get("to") is None or (nv or {}).get("to") < (ov or {}).get("to", ""):
                    bad.append(f"{n['name']}.coverage.to went backwards: {ov} -> {nv}")
            elif ov != nv:
                bad.append(f"{n['name']}.{k}: {ov!r} -> {nv!r}")
    for b in bad:
        print("catalog drift:", b)
    print("catalog --check:", "FAIL" if bad else "ok")
    return 1 if bad else 0


if __name__ == "__main__":
    # Explicit: build() returns a list, and an empty PUBLIC/PRIVATE would make
    # `build() and 0` exit with the list itself — a non-zero, unprintable exit
    # status for a script whose exit code gates a workflow.
    if "--check" in sys.argv:
        raise SystemExit(check())
    build()
    raise SystemExit(0)
