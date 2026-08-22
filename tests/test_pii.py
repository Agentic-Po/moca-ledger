#!/usr/bin/env python3
"""PII gate for the public moca-ledger tree.   # pii-ok

Fails if anything that must stay private (emails, email hashes, mind/human GUIDs,
client IPs, steward domains, geo-attribution, private file names) appears in a   # pii-ok
tracked file. Word-boundary rules so the detector's own vocabulary does not trip it.

Usage:  python3 tests/test_pii.py --tree [path]      (exit 1 on any hit)
"""
import re, sys, os, json, pathlib

ROOT = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else ".").resolve()
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}
# data/*.jsonl are raw chain logs (addresses only) — schema-checked, not text-scanned
SCHEMA_ONLY = {"data"}

DENY = [
    ("email address",        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("64-hex hash (email?)", re.compile(r"\b[0-9a-f]{64}\b")),   # pii-ok
    ("GUID (mind/human id)", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),   # pii-ok
    ("IPv4 address",         re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    ("IPv6 address",         re.compile(r"\b(?:[0-9a-f]{1,4}:){3,}[0-9a-f]{0,4}\b", re.I)),   # pii-ok
    ("private field name",   re.compile(r"\b(steward_email_hash|email_hash|email_domain|ref_party_id|human_id|distinct_id|notes_human_id|steward)\b", re.I)),   # pii-ok
    ("geo attribution",      re.compile(r"\b(surabaya|jakarta|bojonegoro|indonesian|johannesburg)\b", re.I)),   # pii-ok
    ("private domain",       re.compile(r"\b(gmail|outblaze|animocabrands|cryptoslam|gamaa|necub|suarj|tempomail|agentmail|imgfx|animatimg|imageeditgpt|aifotoeditor|theeditai|aniimate|animateany|aminating|aitextextractor|dropcode|bekri|duojumbo|wzjpj|mfxis|anogz|jgkcr|hidesit|ittiv|beiwoh)\.[a-z]{2,}", re.I)),   # pii-ok
    ("action word in public", re.compile(r"(?<![\w/])(freeze[ -]packet|pause creator|kill[- ]switch owner)", re.I)),   # pii-ok
]
BAD_NAMES = re.compile(r"(minds\.csv|humans\.csv|topup\.csv|events_identify|events_raw|bank_transfers\.csv|labels\.json$|identity-lists|warehouse|reconcile-inputs|analysis/|council/)")
ALLOW_LINE = re.compile(r"pii-ok")   # explicit per-line escape hatch

def _public_addresses():
    """Addresses allowed to appear in tracked files (public infrastructure + token
    contracts). Any other bare address is operational intelligence: which wallets we
    flagged, at what value against which threshold, is an oracle for the operator."""
    ok = {"0x" + "0" * 40}
    try:
        d = json.loads((ROOT / "labels" / "public_addresses.json").read_text())
        ok |= {a.lower() for a in d.get("infrastructure", [])}
        ok |= {a.lower() for a in d.get("token_contracts", {}).values()}
        ok |= {a.lower()[:42] for a in d.get("event_topics", {}).values()}
    except Exception:
        pass
    return ok

PUBLIC_ADDR = _public_addresses()
ADDR_RX = re.compile(r"0x[0-9a-f]{40}", re.I)

def files():
    """Everything `git add -A` would publish: tracked files AND untracked files that
    are not ignored.

    Tracked alone is not enough. crawl.yml runs the gate BEFORE the notify step and
    commits with `git add -A` after it, so a file written during the run — or simply
    sitting untracked in the tree, as alerts/msglog/ was — is invisible to a
    tracked-only scan and published by the very same job. Ignored working files
    (live findings, balances) are excluded by --exclude-standard, which is exactly
    the rule git itself applies when deciding what to add."""
    import subprocess
    names = []
    for args in (["git", "ls-files"], ["git", "ls-files", "--others", "--exclude-standard"]):
        try:
            out = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=60)
            names += [n for n in out.stdout.splitlines() if n.strip()]
        except Exception:
            names = []
            break
    if names:
        for n in dict.fromkeys(names):
            p = ROOT / n
            if p.is_file() and not any(part in SKIP_DIRS for part in p.parts): yield p
        return
    for p in ROOT.rglob("*"):                                   # fallback: whole tree
        if not p.is_file():                                     continue
        if any(part in SKIP_DIRS for part in p.parts):          continue
        yield p

def main():
    hits, checked = [], 0
    for p in files():
        rel = str(p.relative_to(ROOT))
        top = rel.split("/")[0]
        if BAD_NAMES.search(rel):
            hits.append((rel, 0, "private file name", rel)); continue
        if top in SCHEMA_ONLY:
            if p.suffix == ".jsonl":                            # schema check, first line only
                try:
                    row = json.loads(p.open().readline() or "{}")
                    extra = set(row) - {"block","ts","tx","li","from","to","value"}
                    if extra: hits.append((rel, 1, "unexpected ledger field", ",".join(sorted(extra))))
                except Exception as e: hits.append((rel, 1, "unreadable ledger row", str(e)[:60]))
            continue
        if p.suffix in {".png",".jpg",".gz",".pyc",".ico"} or p.stat().st_size > 40_000_000: continue
        checked += 1
        try: text = p.read_text(errors="ignore")
        except Exception: continue
        for i, line in enumerate(text.splitlines(), 1):
            if ALLOW_LINE.search(line): continue
            for name, rx in DENY:
                m = rx.search(line)
                if m: hits.append((rel, i, name, m.group(0)[:60]))
            for a in ADDR_RX.findall(line):                      # bare-address rule
                if a.lower() not in PUBLIC_ADDR:
                    hits.append((rel, i, "wallet address (operational intelligence)", a)); break
    if hits:
        print(f"pii: FAIL — {len(hits)} hit(s) in {checked} scanned files")
        for rel, ln, name, frag in hits[:60]: print(f"  {rel}:{ln}  {name}: {frag}")
        return 1
    print(f"pii: OK — {checked} files scanned, 0 hits")
    return 0

if __name__ == "__main__":
    sys.exit(main())
