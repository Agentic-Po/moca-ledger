#!/usr/bin/env python3
"""Operational state lives in the PRIVATE repo, not the public one.

`alerts/state.json` records which wallets were flagged, at what value against
which threshold, and what was already sent. Publishing it is an intelligence
leak; losing it makes the detector re-alert the same findings every run. So it
is fetched from the private repo before detection and pushed back after notify.

Falls back to whatever is on disk when no token is present (local runs), and
degrades loudly rather than silently: if a pull fails in CI the run stops before
notifying, because a stateless run re-sends everything.
"""
import base64, json, os, pathlib, sys, time, urllib.error, urllib.request

ROOT   = pathlib.Path(__file__).resolve().parent.parent
STATE  = ROOT / "alerts" / "state.json"
REPO   = "Agentic-Po/moca-ledger-private"
REMOTE = "state/alerts-state.json"
API    = f"https://api.github.com/repos/{REPO}/contents/{REMOTE}"

def _pat():
    p = os.environ.get("PRIVATE_REPO_PAT")
    if p: return p
    f = pathlib.Path.home() / ".moca-ledger" / "private_repo_pat"
    return f.read_text().strip() if f.exists() else None

def _req(method, body=None):
    pat = _pat()
    if not pat: return None
    r = urllib.request.Request(API, method=method,
                               data=json.dumps(body).encode() if body else None,
                               headers={"Authorization": f"Bearer {pat}",
                                        "Accept": "application/vnd.github+json",
                                        "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=30))

MAX_OPEN   = 600          # findings kept in `open`; older settled ones move to a counter
KEEP_MSGS  = 300          # message_id -> case entries kept for reply targeting
MAX_BYTES  = 900_000      # the Contents API stops serving inline content at 1 MB
MIN_KEEP   = 50           # floor: never let the byte budget empty the state


# A case a person is actively holding open is NOT settled, even though acting on it
# stamps ack_by. Ageing `contained` out first loses the flipped polarity: the next
# fire on that wallet reads as a routine new finding instead of "the fix did not hold".
LIVE_STATUS = ("contained", "reported", "watching")


def _settled(f):
    if f.get("status") in LIVE_STATUS:
        return False
    return bool(f.get("status") == "closed" or f.get("ack_by") or f.get("ack_role"))


def _size(state):
    return len(json.dumps(state, indent=1).encode())


def prune(state):
    """Keep the state small enough that the Contents API will still serve it.

    Above ~1 MB GitHub stops returning inline content and the restore falls back to
    download_url; above that the state is unwieldy either way, and losing it stops
    the detector during exactly the incident it exists for. Two independent limits:

      * a COUNT limit (MAX_OPEN / KEEP_MSGS), and
      * a BYTE limit, because the count alone guarantees nothing — every field added
        to a finding multiplies by the number of open findings (~430 today).

    Settled findings (closed, or acked by a person) age out newest-first; the count
    they represent is kept in `retired` so nothing is dropped without being said."""
    bm = state.get("by_message") or {}
    if len(bm) > KEEP_MSGS:
        state["by_message"] = dict(list(bm.items())[-KEEP_MSGS:])
        print(f"state: trimmed by_message to the last {KEEP_MSGS} entries")

    open_f = state.get("open") or {}
    newest_first = lambda it: str(it[1].get("first_ts") or it[1].get("ts") or "")
    live    = sorted((it for it in open_f.items() if not _settled(it[1])), key=newest_first, reverse=True)
    settled = sorted((it for it in open_f.items() if     _settled(it[1])), key=newest_first, reverse=True)
    order   = live + settled                      # drop from the tail: oldest settled first
    keep, dropped = order[:MAX_OPEN], order[MAX_OPEN:]

    over = False
    while keep:                                   # byte budget, enforced not assumed
        state["open"] = dict(keep)
        if _size(state) <= MAX_BYTES:
            break
        if len(keep) <= MIN_KEEP:
            over = True; break                    # something other than `open` is the bulk
        cut = max(1, min(len(keep) - MIN_KEEP, len(keep) // 10))
        dropped += keep[-cut:]
        keep = keep[:-cut]

    if over:
        print(f"state: WARNING — still {_size(state)} bytes with only {len(keep)} findings kept; "
              f"the bulk is NOT the finding list. Restore will fall back to download_url.")
    if not dropped:
        state["open"] = open_f                    # unchanged: do not churn key order
        return state
    state["open"] = dict(keep)
    state["retired"] = (state.get("retired") or 0) + len(dropped)
    live_dropped = sum(1 for _, f in dropped if not _settled(f))
    print(f"state: retired {len(dropped)} finding(s) to stay under the size cap "
          f"(kept {len(keep)}, {_size(state)} bytes)")
    if live_dropped:
        # Loud on purpose: this is a finding nobody acknowledged being forgotten.
        print(f"state: WARNING — {live_dropped} of them were still UNACKNOWLEDGED; "
              f"the state is at its size ceiling and open cases are being aged out")
    # stdout is a public Actions log nobody is reading at 3am. Council §6.6: never
    # drop a finding without saying so. notify/telegram.py picks this up and posts
    # it on the next run, then clears it.
    n = state.get("retired_notice") or {"total": 0, "unacked": 0}
    state["retired_notice"] = {"total": int(n.get("total", 0)) + len(dropped),
                               "unacked": int(n.get("unacked", 0)) + live_dropped}
    return state


def pull():
    """Fetch state from the private repo into alerts/state.json."""
    if not _pat():
        print("state: no token, using local file"); return True
    try:
        d = _req("GET")
        if d.get("content"):
            raw = base64.b64decode(d["content"])
        else:                                    # >1 MB: the API omits inline content
            url = d.get("download_url")
            if not url:
                print("state: PULL FAILED (no inline content and no download_url) — refusing to run stateless")
                return False
            raw = urllib.request.urlopen(urllib.request.Request(
                url, headers={"Authorization": f"Bearer {_pat()}"}), timeout=30).read()
            print(f"state: inline content omitted (>1 MB) — restored {len(raw)} bytes via download_url")
        json.loads(raw)                          # never write a file we cannot parse
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_bytes(raw)
        (STATE.parent / ".state_sha").write_text(d["sha"])
        n = len(json.loads(raw).get("open", {}))
        print(f"state: pulled {n} findings from the private repo")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("state: none stored yet — first run"); return True
        print(f"state: PULL FAILED ({e.code}) — refusing to run stateless"); return False
    except Exception as e:
        print(f"state: PULL FAILED ({type(e).__name__}) — refusing to run stateless"); return False


def _remote_sha():
    try:
        return (_req("GET") or {}).get("sha")
    except Exception:
        return None


def _remote_state():
    """(state dict, sha) as it is on the remote right now, or None."""
    try:
        d = _req("GET") or {}
        if d.get("content"):
            raw = base64.b64decode(d["content"])
        else:
            url = d.get("download_url")
            if not url: return None
            raw = urllib.request.urlopen(urllib.request.Request(
                url, headers={"Authorization": f"Bearer {_pat()}"}), timeout=30).read()
        return json.loads(raw), d.get("sha")
    except Exception as e:
        print(f"state: could not read the concurrent write ({type(e).__name__})")
        return None


# Fields recording a decision a PERSON made. If either side of a concurrent write
# has them and the other does not, they survive: erasing a human's `contained` is
# the worst outcome a merge can produce.
HUMAN_FIELDS = ("status", "status_ts", "status_by", "status_note", "value_at_status",
                "ack_by", "ack_ts", "ack_note", "ack_role", "snooze_until")


def merge(remote, local):
    """Combine a concurrently-written remote state with ours, conservatively.

    Refreshing the sha and re-PUTting the same payload turned optimistic
    concurrency into last-writer-wins: two workflows in different concurrency
    groups (crawl */10 and selftest) could each pull, edit and push, and whichever
    landed second reverted the other wholesale — re-arming `pending_send` on alerts
    already delivered, or erasing a status a human had just set.

    The rules, in order of what must never be lost:
      * a human decision on either side wins,
      * a finding recorded as SENT on either side stays sent (never re-page),
      * a finding present on only one side is kept (never lose a new detection),
      * `telegram_offset` takes the higher value (never replay handled updates),
      * `by_message` and `muted` are unioned, ours winning on a clash.

    Cost of the third rule: a key the LOCAL side deliberately deleted (the daily
    self-test sweeping its synthetic finding) comes back if the remote still has
    it. That is a cosmetic loss — the next sweep removes it — and it is the safe
    side of the trade."""
    out = dict(remote)
    out.update({k: v for k, v in local.items() if k not in ("open", "by_message", "muted",
                                                            "telegram_offset")})
    out["telegram_offset"] = max(int(remote.get("telegram_offset") or 0),
                                 int(local.get("telegram_offset") or 0))
    for field in ("by_message", "muted"):
        m = dict(remote.get(field) or {}); m.update(local.get(field) or {})
        if m: out[field] = m
    ro, lo = remote.get("open") or {}, local.get("open") or {}
    merged = dict(lo)
    for k, rf in ro.items():
        lf = merged.get(k)
        if lf is None:
            merged[k] = rf; continue
        cur = dict(lf)
        if rf.get("status") and not lf.get("status"):
            for fld in HUMAN_FIELDS:
                if fld in rf: cur[fld] = rf[fld]
        for fld in ("ack_by", "ack_ts", "enrich_requested", "tg_message_id"):
            if rf.get(fld) and not cur.get(fld): cur[fld] = rf[fld]
        if rf.get("last_sent") and not cur.get("last_sent"):
            cur["last_sent"] = rf["last_sent"]; cur["pending_send"] = False
        merged[k] = cur
    out["open"] = merged
    return out


def push():
    """Store the updated state back in the private repo.

    Retries; on a conflict it MERGES with whatever landed in between rather than
    overwriting it (see merge()). A dropped push means the next run re-pulls an
    older copy, re-sends alerts the human already handled and re-dispatches
    enrichment. Returns False loudly rather than pretending it worked."""
    if not _pat() or not STATE.exists():
        print("state: nothing to push"); return True
    st = prune(json.loads(STATE.read_text()))
    STATE.write_text(json.dumps(st, indent=1))
    sha_f = STATE.parent / ".state_sha"
    sha = sha_f.read_text().strip() if sha_f.exists() else None
    detail = "unknown"
    for attempt in range(3):
        content = base64.b64encode(json.dumps(st, indent=1).encode()).decode()
        body = {"message": f"state {os.environ.get('GITHUB_RUN_ID','local')}", "content": content}
        if sha: body["sha"] = sha
        try:
            d = _req("PUT", body)
            sha_f.write_text(d["content"]["sha"])
            STATE.write_text(json.dumps(st, indent=1))
            print(f"state: pushed {len(st.get('open', {}))} findings, {STATE.stat().st_size} bytes")
            return True
        except urllib.error.HTTPError as e:
            detail = f"http {e.code}"
            if e.code in (409, 422):          # somebody else wrote in between
                cur = _remote_state()
                if cur is not None:
                    before = len(st.get("open", {}))
                    st = prune(merge(cur[0], st))
                    sha = cur[1]
                    print(f"state: conflict — merged with the concurrent write "
                          f"({before} -> {len(st.get('open', {}))} findings)")
                else:
                    sha = _remote_sha()
        except Exception as e:
            detail = type(e).__name__
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    print(f"state: PUSH FAILED ({detail}) after 3 attempts — the next run will re-pull an "
          f"older copy and may re-send handled alerts")
    return False


if __name__ == "__main__":
    ok = pull() if sys.argv[1:] == ["pull"] else push()
    sys.exit(0 if ok else 1)
