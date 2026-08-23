#!/usr/bin/env python3
"""Durable message ledger — every message in and out, nothing dropped in silence.

Why this exists: association used to live in `alerts/state.json` under `by_message`,
which (a) only recorded alerts sent through send_pending, (b) is pruned to the last
KEEP_MSGS entries to keep the state file under the Contents API size limit, and (c)
recorded nothing about messages that were not cases. So a reply to a digest, a notice,
a tier-2 detail, or any older alert was unmatchable — and an unmatched reply was
discarded. A person wrote something at 01:32 and it vanished.

TWO STRUCTURES, ON PURPOSE
--------------------------
The first version of this file promised "append-only, nothing is ever deleted" and
then pushed one unbounded file as a whole-file base64 PUT — re-creating the exact 1 MB
ceiling it was built to escape — and re-read and re-parsed that whole file once per
lookup, recursing four deep (fix-round critic #7). Splitting the two jobs fixes both:

  * the ARCHIVE (`messages/out-YYYY-MM-NN.jsonl`, `in-…`) is append-only and complete.
    It is sharded by month and by SHARD_MAX bytes, so the file being written is always
    small; a sealed shard is immutable and is never rewritten or re-uploaded.
  * the INDEX (`messages/index.json`) is what resolution reads: a bounded map of the
    last INDEX_KEEP outbound message ids to (kind, case_id, reply_to). It is loaded
    once per process, not once per lookup.

The index is bounded, so it is honest about its own edge: an id older than INDEX_KEEP
that is not in a locally-present shard resolves to None, and the caller says so rather
than guessing. It holds ~13x what `by_message` did.

Files (private repo — message text carries human words and Telegram user ids):
  messages/index.json           bounded lookup: message_id -> [kind, case_id, reply_to]
  messages/out-YYYY-MM-NN.jsonl one row per message the bot sent
  messages/in-YYYY-MM-NN.jsonl  one row per message a human sent to the bot
"""
import atexit, base64, json, os, pathlib, sys, urllib.request, urllib.error, datetime as dt

ROOT   = pathlib.Path(__file__).resolve().parent.parent
LOCAL  = ROOT / "alerts" / "msglog"
INDEX  = LOCAL / "index.json"
REPO   = "Agentic-Po/moca-ledger-private"
REMOTE_DIR = "messages"

INDEX_KEEP = 4000       # outbound ids kept for resolution (by_message kept 300)
SHARD_MAX  = 600_000    # bytes; above this the stream rolls to the next shard
LEGACY     = ("outbound.jsonl", "inbound.jsonl")   # the pre-shard files, read once
LINKS      = "pending-links.jsonl"   # associations handed over by the private side
LINKS_TAIL = 2000                    # rows read from it per pull — bounded work

_INDEX = None           # {"v":1, "msgs": {...}, "legacy": bool}
_DIRTY = False


# ---------------------------------------------------------------- github contents

def _pat():
    p = os.environ.get("PRIVATE_REPO_PAT")
    if p:
        return p
    f = pathlib.Path.home() / ".moca-ledger" / "private_repo_pat"
    return f.read_text().strip() if f.exists() else None


def _api(path, method="GET", body=None):
    pat = _pat()
    if not pat:
        return None
    r = urllib.request.Request(f"https://api.github.com/repos/{REPO}/contents/{path}", method=method,
                               data=json.dumps(body).encode() if body else None,
                               headers={"Authorization": f"Bearer {pat}",
                                        "Accept": "application/vnd.github+json",
                                        "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=30))


def _fetch(path):
    """File bytes, or None on 404. Falls back to download_url: the Contents API stops
    returning inline content above 1 MB, which is precisely when a ledger matters."""
    try:
        d = _api(path)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if d is None:
        return None
    if d.get("content"):
        return base64.b64decode(d["content"]), d.get("sha")
    req = urllib.request.Request(d["download_url"], headers={"Authorization": f"Bearer {_pat()}"})
    return urllib.request.urlopen(req, timeout=30).read(), d.get("sha")


def _put(path, data, sha=None):
    body = {"message": f"msglog {os.environ.get('GITHUB_RUN_ID', 'local')}",
            "content": base64.b64encode(data).decode()}
    if sha:
        body["sha"] = sha
    return _api(path, "PUT", body)


# ---------------------------------------------------------------- shards

def _now():
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shards(stream):
    """Local shard paths for a stream, oldest first. Names sort chronologically."""
    LOCAL.mkdir(parents=True, exist_ok=True)
    return sorted(LOCAL.glob(f"{stream}-*.jsonl"))


def _open_shard(stream):
    """The shard to append to: newest of THIS month, rolled when it passes SHARD_MAX.

    A sealed shard is never touched again, which is what keeps `push` bounded no
    matter how long the ledger runs."""
    month = dt.datetime.now(dt.UTC).strftime("%Y-%m")
    mine = sorted(LOCAL.glob(f"{stream}-{month}-*.jsonl"))
    if mine and mine[-1].stat().st_size < SHARD_MAX:
        return mine[-1]
    nxt = int(mine[-1].stem.rsplit("-", 1)[-1]) + 1 if mine else 1
    LOCAL.mkdir(parents=True, exist_ok=True)
    return LOCAL / f"{stream}-{month}-{nxt:02d}.jsonl"


def _append(stream, row):
    p = _open_shard(stream)
    with open(p, "a") as fh:
        fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")


def _rows(stream):
    out = []
    for p in _shards(stream):
        for line in p.read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


# ---------------------------------------------------------------- index

def _index():
    global _INDEX
    if _INDEX is None:
        try:
            _INDEX = json.loads(INDEX.read_text())
        except Exception:
            _INDEX = {"v": 1, "msgs": {}}
        _INDEX.setdefault("msgs", {})
    return _INDEX


def _remember(message_id, kind, case_id=None, reply_to=None):
    global _DIRTY
    ix = _index()
    ix["msgs"][str(int(message_id))] = [kind, case_id, reply_to]
    if len(ix["msgs"]) > INDEX_KEEP:
        # Trimmed from the front: dicts keep insertion order, and the archive shards
        # still hold every row. Only the fast path is bounded.
        ix["msgs"] = dict(list(ix["msgs"].items())[-INDEX_KEEP:])
    _DIRTY = True


def flush():
    """Write the index once, at exit, rather than on every row."""
    global _DIRTY
    if not _DIRTY:
        return
    LOCAL.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_index(), separators=(",", ":")))
    os.replace(tmp, INDEX)
    _DIRTY = False


atexit.register(flush)


# ---------------------------------------------------------------- recording

KINDS = ("alert", "chart", "digest", "tier2", "notice", "confirm", "health", "test", "linked")


def record_out(message_id, kind, case_id=None, reply_to=None, **meta):
    """Record a message the bot sent, whether or not it is about a case.

    Never raises: this is called from inside the send path, and losing an alert to
    a bookkeeping error would be worse than losing the bookkeeping."""
    try:
        if not message_id:
            return
        mid = int(message_id)
        _append("out", {"ts": _now(), "message_id": mid, "kind": kind, "case_id": case_id,
                        "reply_to": reply_to,
                        **{k: v for k, v in meta.items() if v is not None}})
        _remember(mid, kind, case_id, reply_to)
    except Exception as e:                                    # never silent
        print(f"msglog: record_out failed for {message_id} ({type(e).__name__})", file=sys.stderr)


def record_in(update, resolved_case=None, outcome="seen", action=None):
    """Record a message a human sent, matched or not. Nothing is discarded."""
    try:
        m = (update or {}).get("message") or {}
        _append("in", {"ts": _now(), "update_id": (update or {}).get("update_id"),
                       "message_id": m.get("message_id"), "sent_at": m.get("date"),
                       "from": (m.get("from") or {}).get("id"),
                       "text": (m.get("text") or "")[:500],
                       "reply_to": (m.get("reply_to_message") or {}).get("message_id"),
                       "resolved_case": resolved_case, "outcome": outcome, "action": action})
    except Exception as e:
        print(f"msglog: record_in failed ({type(e).__name__})", file=sys.stderr)


# ---------------------------------------------------------------- resolution

def _entry(message_id):
    try:
        mid = str(int(message_id))
    except (TypeError, ValueError):
        return None
    e = _index()["msgs"].get(mid)
    if e:
        return e
    # Not in the bounded index. The archive shards we hold locally are the fallback;
    # one pass, cached on the index object so a chain of four costs one read.
    ix = _index()
    if "_scan" not in ix:
        scan = {}
        for r in _rows("out"):
            if r.get("message_id"):
                scan[str(r["message_id"])] = [r.get("kind"), r.get("case_id"), r.get("reply_to")]
        ix["_scan"] = scan
    return ix["_scan"].get(mid)


def resolve(message_id, depth=0):
    """Which case does this message belong to, or None?

    Follows the parent chain, so a reply to a tier-2 detail — which is itself a reply
    to the alert — still resolves to the alert's case."""
    if not message_id or depth > 4:
        return None
    e = _entry(message_id)
    if not e:
        return None
    kind, case_id, reply_to = (e + [None, None, None])[:3]
    if case_id:
        return case_id
    if reply_to:
        return resolve(reply_to, depth + 1)
    return None


def describe(message_id):
    """What WAS the message being replied to? Lets the bot say "that was the daily
    digest, which is not a case" instead of a bare "I cannot tell"."""
    e = _entry(message_id)
    return e[0] if e else None


KIND_WORDS = {"alert": "an alert", "chart": "the chart for an alert",
              "digest": "the quiet daily digest", "tier2": "a tier-2 detail",
              "notice": "a notice from me", "confirm": "one of my own confirmations",
              "health": "a health notice", "test": "a self-test message",
              "linked": "a message linked to a case by hand"}


def describe_words(message_id):
    """The same answer in the channel's own English, or None."""
    return KIND_WORDS.get(describe(message_id))


def unresolved(limit=20):
    """Replies we could not match — kept so they can be linked afterwards."""
    return [r for r in _rows("in") if r.get("outcome") == "unmatched"][-limit:]


def link(message_id, case_id):
    """Retroactively attach a message — and anything replying to it — to a case."""
    record_out(message_id, "linked", case_id=case_id, note="linked after the fact")
    return case_id


# ---------------------------------------------------------------- sync

def _seen_path():
    # Derived at call time, not at import: the test harness redirects LOCAL.
    return LOCAL / ".pushed.json"      # {name: {"sha": ..., "size": N}} — what the remote holds


def _seen():
    try:
        return json.loads(_seen_path().read_text())
    except Exception:
        return {}


def _mark(name, sha, size):
    d = _seen()
    d[name] = {"sha": sha, "size": size}
    LOCAL.mkdir(parents=True, exist_ok=True)
    _seen_path().write_text(json.dumps(d, separators=(",", ":")))


def _pull_file(name):
    got = _fetch(f"{REMOTE_DIR}/{name}")
    if not got:
        return None
    data, sha = got
    LOCAL.mkdir(parents=True, exist_ok=True)
    (LOCAL / name).write_bytes(data)
    _mark(name, sha, len(data))
    return data


def _merge_lines(local_bytes, remote_bytes):
    """Union by line, remote first, order preserved. Both sides are append-only, so
    the union IS the truth — never the last writer's copy."""
    seen, out = set(), []
    for blob in (remote_bytes, local_bytes):
        for line in (blob or b"").decode(errors="replace").splitlines():
            if line.strip() and line not in seen:
                seen.add(line); out.append(line)
    return ("\n".join(out) + "\n").encode()


def _merge_index(local_msgs, remote_bytes):
    try:
        merged = json.loads(remote_bytes or b"{}")
    except Exception:
        merged = {}
    merged.setdefault("v", 1)
    msgs = merged.get("msgs") or {}
    msgs.update(local_msgs)                       # ours wins on a clash: it is newer
    merged["msgs"] = dict(list(msgs.items())[-INDEX_KEEP:])
    merged.pop("_scan", None)
    merged["legacy"] = True
    return json.dumps(merged, separators=(",", ":")).encode()


def pull():
    """Bring down the index and the shard each stream is currently writing.

    Sealed shards are immutable history and are deliberately NOT downloaded: they
    would grow every run forever, and resolution reads the index."""
    global _INDEX
    if not _pat():
        print("msglog: no token, local only"); return True
    ok = True
    try:
        listing = _api(REMOTE_DIR) or []
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("msglog: no remote ledger yet"); return True
        print(f"msglog: pull listing failed (http {e.code})"); return False
    except Exception as e:
        print(f"msglog: pull listing failed ({type(e).__name__})"); return False

    names = [f["name"] for f in listing if f.get("type") == "file"]
    want = ["index.json"] if "index.json" in names else []
    for stream in ("out", "in"):
        shards = sorted(n for n in names if n.startswith(f"{stream}-") and n.endswith(".jsonl"))
        if shards:
            want.append(shards[-1])
    for name in want:
        try:
            _pull_file(name)
        except Exception as e:
            print(f"msglog: pull {name} failed ({type(e).__name__})"); ok = False
    _INDEX = None                                   # re-read what we just pulled

    # One-time: fold the pre-shard files into the index so the ids they already hold
    # stay resolvable. They are never written to again.
    ix = _index()
    if not ix.get("legacy"):
        for name in LEGACY:
            if name not in names:
                continue
            try:
                data, _ = _fetch(f"{REMOTE_DIR}/{name}")
                for line in data.decode(errors="replace").splitlines():
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    if r.get("message_id"):
                        _remember(r["message_id"], r.get("kind") or "alert",
                                  r.get("case_id"), r.get("reply_to"))
                print(f"msglog: folded legacy {name} into the index")
            except Exception as e:
                print(f"msglog: legacy {name} not folded ({type(e).__name__})"); ok = False
        ix["legacy"] = True
        globals()["_DIRTY"] = True

    # Tier-2 details are sent by the PRIVATE repo, which commits with git while this
    # pushes through the Contents API; writing one shard from both sides would race.
    # So the private side appends its associations to pending-links.jsonl and they are
    # folded in here. Idempotent — re-folding a row rewrites the same entry — and that
    # file is itself the private-side archive of those rows, so reading only its tail
    # loses nothing.
    if LINKS in names:
        try:
            data, _ = _fetch(f"{REMOTE_DIR}/{LINKS}")
            n = 0
            for line in data.decode(errors="replace").splitlines()[-LINKS_TAIL:]:
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get("message_id"):
                    _remember(r["message_id"], r.get("kind") or "tier2",
                              r.get("case_id"), r.get("reply_to"))
                    n += 1
            print(f"msglog: folded {n} private-side association(s)")
        except Exception as e:
            print(f"msglog: pending links not folded ({type(e).__name__})"); ok = False
    flush()
    print(f"msglog: pull done ({len(ix['msgs'])} indexed; {', '.join(want) or 'nothing remote'})")
    return ok


def push():
    """Upload the index and any shard whose bytes differ from what the remote holds.

    Every local shard is considered, not just the open one: a shard that filled up
    mid-run is sealed and rolled, and pushing only the open shard would leave its
    last rows on a disposable CI runner. A sealed shard matches its recorded size
    from then on and is skipped for free."""
    if not _pat():
        print("msglog: no token, local only"); return True
    flush()
    ok, sent = True, []
    files = [INDEX] + _shards("out") + _shards("in")
    for p in files:
        if not p.exists():
            continue
        name, data = p.name, p.read_bytes()
        known = _seen().get(name) or {}
        if known.get("size") == len(data) and known.get("sha"):
            continue                                  # the remote already has exactly this
        sha = known.get("sha")
        for attempt in range(2):
            try:
                d = _put(f"{REMOTE_DIR}/{name}", data, sha)
                _mark(name, d["content"]["sha"], len(data))
                sent.append(name)
                break
            except urllib.error.HTTPError as e:
                if e.code in (409, 422) and attempt == 0:
                    # Another run wrote in between. Append-only on both sides, so merge.
                    got = _fetch(f"{REMOTE_DIR}/{name}")
                    if not got:
                        sha = None; continue
                    remote, sha = got
                    data = (_merge_index(_index()["msgs"], remote) if name == "index.json"
                            else _merge_lines(data, remote))
                    p.write_bytes(data)
                    continue
                print(f"msglog: push {name} failed (http {e.code})"); ok = False; break
            except Exception as e:
                print(f"msglog: push {name} failed ({type(e).__name__})"); ok = False; break
    print(f"msglog: push done ({', '.join(sent) or 'nothing new'}, {len(_index()['msgs'])} indexed)")
    return ok


def sync(direction="pull"):
    return pull() if direction == "pull" else push()


def stats():
    ix = _index()
    return {"indexed": len(ix.get("msgs") or {}),
            "index_cap": INDEX_KEEP,
            "shards_out": [p.name for p in _shards("out")],
            "shards_in": [p.name for p in _shards("in")],
            "unresolved": len(unresolved(10 ** 9))}


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["pull"]:    sys.exit(0 if pull() else 1)
    if a[:1] == ["push"]:    sys.exit(0 if push() else 1)
    if a[:1] == ["resolve"]: print(resolve(a[1])); sys.exit(0)
    if a[:1] == ["describe"]: print(describe(a[1])); sys.exit(0)
    if a[:1] == ["link"]:    print(link(a[1], a[2])); flush(); sys.exit(0)
    print(json.dumps(stats(), indent=1))
