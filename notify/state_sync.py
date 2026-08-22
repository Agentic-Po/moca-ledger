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
import base64, json, os, pathlib, sys, urllib.request

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

MAX_OPEN   = 600          # findings kept in `open`; older resolved ones move to a counter
KEEP_MSGS  = 300          # message_id -> case entries kept for reply targeting


def prune(state):
    """Keep the state small enough that the Contents API will still serve it.

    Above ~1 MB GitHub stops returning inline content and the restore fails, which
    stops the detector during exactly the incident it exists for. Resolved and
    acknowledged findings age out; the count they represent is kept."""
    import datetime as dt
    open_f = state.get("open") or {}
    if len(open_f) <= MAX_OPEN and len(state.get("by_message") or {}) <= KEEP_MSGS:
        return state
    def sort_key(item):
        f = item[1]
        settled = bool(f.get("status") in ("closed",) or f.get("ack_by") or f.get("ack_role"))
        return (0 if not settled else 1, str(f.get("first_ts") or ""))
    keep = dict(sorted(open_f.items(), key=sort_key, reverse=False)[:MAX_OPEN])
    dropped = len(open_f) - len(keep)
    if dropped > 0:
        state["retired"] = (state.get("retired") or 0) + dropped
        state["open"] = keep
        print(f"state: retired {dropped} settled findings (kept {len(keep)})")
    bm = state.get("by_message") or {}
    if len(bm) > KEEP_MSGS:
        state["by_message"] = dict(list(bm.items())[-KEEP_MSGS:])
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
            raw = urllib.request.urlopen(urllib.request.Request(
                url, headers={"Authorization": f"Bearer {_pat()}"}), timeout=30).read()
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

def push():
    """Store the updated state back in the private repo."""
    if not _pat() or not STATE.exists():
        print("state: nothing to push"); return True
    st = prune(json.loads(STATE.read_text()))
    STATE.write_text(json.dumps(st, indent=1))
    body = {"message": f"state {os.environ.get('GITHUB_RUN_ID','local')}",
            "content": base64.b64encode(STATE.read_bytes()).decode()}
    sha_f = STATE.parent / ".state_sha"
    if sha_f.exists(): body["sha"] = sha_f.read_text().strip()
    try:
        d = _req("PUT", body)
        (STATE.parent / ".state_sha").write_text(d["content"]["sha"])
        print(f"state: pushed {len(json.loads(STATE.read_text()).get('open', {}))} findings")
        return True
    except Exception as e:
        print(f"state: push failed ({type(e).__name__}) — next run will re-pull the older copy"); return False

if __name__ == "__main__":
    ok = pull() if sys.argv[1:] == ["pull"] else push()
    sys.exit(0 if ok else 1)
