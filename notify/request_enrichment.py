#!/usr/bin/env python3
"""Ask the private side to tier-2 enrich any finding that has been alerted but
not yet enriched. Sends a repository_dispatch; no-op without PRIVATE_REPO_PAT
(the private side also polls hourly as a safety net).

`enrich_requested` is the only thing stopping the same finding being dispatched
every ten minutes forever, and it only survives if the state is pushed. So the
push result is checked, not discarded: a lost push is reported as a re-dispatch
that WILL happen, and the exit code goes non-zero.
"""
import json, os, pathlib, sys, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "alerts" / "state.json"
TARGET = "Agentic-Po/moca-ledger-private"


def dispatch(finding_id, pat, target=TARGET):
    """repository_dispatch one finding. Returns (ok, detail) — never swallows."""
    body = json.dumps({"event_type": "finding",
                       "client_payload": {"finding_id": finding_id}}).encode()
    req = urllib.request.Request(f"https://api.github.com/repos/{target}/dispatches", data=body,
                                 headers={"Authorization": f"Bearer {pat}",
                                          "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=20)
        code = getattr(r, "status", None) or r.getcode()
        return (code in (200, 202, 204)), f"http {code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:60]}"


def pending(state):
    return [f for f in (state.get("open") or {}).values()
            if f.get("tier") in ("page", "notify") and not f.get("pending_send")
            and f.get("ack_by") != "go-live-seed" and not f.get("enrich_requested") and f.get("id")]


def main():
    pat = os.environ.get("PRIVATE_REPO_PAT")
    s = json.loads(STATE.read_text()) if STATE.exists() else {}
    todo = pending(s)
    if not todo:
        print("enrichment: nothing to request"); return 0
    if not pat:
        print(f"enrichment: {len(todo)} pending, no PAT — private side will pick them up on its hourly pass")
        return 0
    sent, failed = 0, 0
    for f in todo[:10]:
        ok, detail = dispatch(f["id"], pat)
        if ok:
            f["enrich_requested"] = True; sent += 1
        else:
            failed += 1
            print(f"enrichment: dispatch failed for {f['id']} ({detail})")
    STATE.write_text(json.dumps(s, indent=1))
    rc = 0
    if sent:
        # Persist immediately. This step runs after the state was already saved, so
        # without this push the flag is erased by the next pull() and every run
        # re-dispatches the same findings, silently, forever.
        pushed = False
        try:
            sys.path.insert(0, str(ROOT))
            from notify.state_sync import push as push_state
            pushed = bool(push_state())
        except Exception as e:
            print(f"enrichment: could not persist flags ({type(e).__name__})")
        if not pushed:
            print(f"enrichment: STATE PUSH FAILED — the enrich_requested flag for {sent} finding(s) "
                  f"is NOT persisted and the next run WILL re-dispatch them")
            rc = 1
    if failed:
        rc = 1
    print(f"enrichment: requested {sent}, failed {failed}, still pending {max(0, len(todo) - sent)}")
    return rc


if __name__ == "__main__": sys.exit(main())
