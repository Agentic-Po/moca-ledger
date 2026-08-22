#!/usr/bin/env python3
"""Durable message ledger — every message in and out, never pruned, never lost.

Why this exists: association used to live in `alerts/state.json` under `by_message`,
which (a) only recorded alerts sent through send_pending, (b) was pruned to keep the
state file under the API size limit, and (c) recorded nothing about messages that were
not cases. So a reply to a digest, a notice, a tier-2 detail, or any older alert was
unmatchable — and an unmatched reply was discarded. A person wrote something at 01:32
and it vanished.

Design rules:
  * append-only; nothing is ever deleted or rewritten
  * lives in the PRIVATE repo (message text can contain identity data)
  * resolution is layered: direct map -> ledger -> parent chain -> unresolved queue
  * an unresolved reply is STORED, not dropped, and can be linked afterwards

Files (private repo):
  messages/outbound.jsonl   one row per message the bot sent
  messages/inbound.jsonl    one row per message a human sent to the bot
"""
import json, os, pathlib, base64, urllib.request, datetime as dt

ROOT     = pathlib.Path(__file__).resolve().parent.parent
LOCAL    = ROOT / "alerts" / "msglog"
OUT      = LOCAL / "outbound.jsonl"
IN       = LOCAL / "inbound.jsonl"
REPO     = "Agentic-Po/moca-ledger-private"
REMOTE   = {"outbound.jsonl": "messages/outbound.jsonl", "inbound.jsonl": "messages/inbound.jsonl"}


def _pat():
    p = os.environ.get("PRIVATE_REPO_PAT")
    if p: return p
    f = pathlib.Path.home() / ".moca-ledger" / "private_repo_pat"
    return f.read_text().strip() if f.exists() else None


def _api(path, method="GET", body=None):
    pat = _pat()
    if not pat: return None
    r = urllib.request.Request(f"https://api.github.com/repos/{REPO}/contents/{path}", method=method,
                               data=json.dumps(body).encode() if body else None,
                               headers={"Authorization": f"Bearer {pat}",
                                        "Accept": "application/vnd.github+json",
                                        "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=30))


def _now():
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append(path, row):
    LOCAL.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")


def record_out(message_id, kind, case_id=None, **meta):
    """Record every message the bot sends, whether or not it is about a case.

    kind: alert | digest | tier2 | notice | confirm | health | test
    """
    if not message_id: return
    _append(OUT, {"ts": _now(), "message_id": int(message_id), "kind": kind,
                  "case_id": case_id, **{k: v for k, v in meta.items() if v is not None}})


def record_in(update, resolved_case=None, outcome="seen", action=None):
    """Record every message a human sends, matched or not. Nothing is discarded."""
    m = (update or {}).get("message") or {}
    _append(IN, {"ts": _now(), "update_id": update.get("update_id"),
                 "message_id": m.get("message_id"), "sent_at": m.get("date"),
                 "from": (m.get("from") or {}).get("id"),
                 "text": (m.get("text") or "")[:500],
                 "reply_to": (m.get("reply_to_message") or {}).get("message_id"),
                 "resolved_case": resolved_case, "outcome": outcome, "action": action})


def _rows(path):
    if not path.exists(): return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try: out.append(json.loads(line))
            except json.JSONDecodeError: continue
    return out


def resolve(message_id, depth=0):
    """Which case does this message belong to? Follows the parent chain, so a reply
    to a tier-2 detail (which itself replied to the alert) still resolves."""
    if not message_id or depth > 4: return None
    for r in reversed(_rows(OUT)):
        if r.get("message_id") == int(message_id):
            if r.get("case_id"): return r["case_id"]
            if r.get("reply_to"): return resolve(r["reply_to"], depth + 1)
            return None
    return None


def describe(message_id):
    """What WAS the message being replied to? Lets the bot say 'that was the daily
    digest, which is not a case' instead of a bare 'cannot match'."""
    for r in reversed(_rows(OUT)):
        if r.get("message_id") == int(message_id or 0):
            return r.get("kind")
    return None


def unresolved(limit=20):
    """Replies we could not match — kept so they can be linked later."""
    return [r for r in _rows(IN) if r.get("outcome") == "unmatched"][-limit:]


def link(message_id, case_id):
    """Retroactively attach a message (and any replies to it) to a case."""
    record_out(message_id, "linked", case_id=case_id, note="linked after the fact")
    return case_id


def sync(direction="pull"):
    """Mirror the ledger to the private repo. Append-only, so a pull merges by line."""
    if not _pat():
        print("msglog: no token, local only"); return True
    ok = True
    for name, remote in REMOTE.items():
        local = LOCAL / name
        try:
            if direction == "pull":
                try:
                    d = _api(remote)
                    raw = base64.b64decode(d["content"]) if d.get("content") else \
                        urllib.request.urlopen(urllib.request.Request(
                            d["download_url"], headers={"Authorization": f"Bearer {_pat()}"}), timeout=30).read()
                    LOCAL.mkdir(parents=True, exist_ok=True)
                    have = set(local.read_text().splitlines()) if local.exists() else set()
                    merged = [l for l in raw.decode().splitlines() if l.strip()]
                    for l in sorted(have):                      # keep anything only we have
                        if l not in merged: merged.append(l)
                    local.write_text("\n".join(merged) + "\n")
                    (LOCAL / f".{name}.sha").write_text(d["sha"])
                except urllib.error.HTTPError as e:
                    if e.code != 404: raise
            else:
                if not local.exists(): continue
                body = {"message": f"msglog {os.environ.get('GITHUB_RUN_ID', 'local')}",
                        "content": base64.b64encode(local.read_bytes()).decode()}
                shaf = LOCAL / f".{name}.sha"
                if shaf.exists(): body["sha"] = shaf.read_text().strip()
                d = _api(remote, "PUT", body)
                shaf.write_text(d["content"]["sha"])
        except Exception as e:
            print(f"msglog: {direction} {name} failed ({type(e).__name__})"); ok = False
    counts = f"{len(_rows(OUT))} out / {len(_rows(IN))} in"
    print(f"msglog: {direction} done ({counts})")
    return ok


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["pull"]:   sys.exit(0 if sync("pull") else 1)
    if sys.argv[1:] == ["push"]:   sys.exit(0 if sync("push") else 1)
    if sys.argv[1:2] == ["resolve"]: print(resolve(sys.argv[2])); sys.exit(0)
    print(json.dumps({"outbound": len(_rows(OUT)), "inbound": len(_rows(IN)),
                      "unresolved": len(unresolved(9999))}, indent=1))
