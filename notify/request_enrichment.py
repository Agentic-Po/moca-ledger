#!/usr/bin/env python3
"""Ask the private side to tier-2 enrich any finding that has been alerted but
not yet enriched. Sends a repository_dispatch; no-op without PRIVATE_REPO_PAT
(the private side also polls hourly as a safety net).

`enrich_requested` is the only thing stopping the same finding being dispatched
every ten minutes forever, and it only survives if the state is pushed. So the
push result is checked, not discarded: a lost push is reported as a re-dispatch
that WILL happen, and the exit code goes non-zero.
"""
import json, os, pathlib, sys, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(1, str(ROOT))
try:                              # public Actions logs: no finding ids, no counts
    from privlog import private_print
except ImportError:
    sys.path.insert(1, str(ROOT))
    from notify.privlog import private_print
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


def pending(state, now=None):
    if state.get("triage_version"):
        from notify.triage import stamp
        now = time.time() if now is None else now
        return [f for f in (state.get("open") or {}).values()
                if f.get("needs_triage") and (not f.get("triage_requested")
                    or now - max(stamp(f.get("triage_ts")), stamp(state.get("private_dispatch_at"))) >= 3600)
                and f.get("status") not in ("closed", "archived") and f.get("id")]
    return [f for f in (state.get("open") or {}).values()
            if f.get("tier") in ("page", "notify") and not f.get("pending_send")
            and f.get("ack_by") != "go-live-seed" and not f.get("enrich_requested") and f.get("id")]


def main():
    pat = os.environ.get("PRIVATE_REPO_PAT")
    s = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = time.time()
    todo = pending(s, now)
    triage = bool(s.get("triage_version"))
    elapsed = now - float(s.get("private_dispatch_at") or 0)
    maintenance = triage and elapsed >= 6 * 3600
    urgent = any(not f.get("triage_requested") and (f.get("signal") in {"S-X", "4b", "S-C"}
                 or (f.get("tier") == "page" and not f.get("shadow_of"))) for f in todo)
    if triage and elapsed < 50 * 60 and not urgent:
        private_print("enrichment: batch cooldown", public="enrichment: ok"); return 0
    if not todo and not maintenance:
        private_print("enrichment: nothing to request", public="enrichment: ok"); return 0
    if not pat:
        private_print(f"enrichment: {len(todo)} pending, no PAT — private side will pick them up when its scheduled job runs",
                      public="enrichment: no PAT — the private side has a scheduled safety net")
        return 0
    sent, failed = 0, 0
    batch = todo if s.get("triage_version") else todo[:10]
    for f in ((batch[:1] or [{}]) if triage else batch):
        ok, detail = dispatch("" if s.get("triage_version") else f["id"], pat)
        if ok:
            if s.get("triage_version"):
                for candidate in batch:
                    candidate["triage_requested"] = True
                s["private_dispatch_at"] = now
                sent += 1
            else:
                f["enrich_requested"] = True; sent += 1
        else:
            failed += 1
            private_print(f"enrichment: dispatch failed for {f.get('id', 'maintenance')} ({detail})")
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
            private_print(f"enrichment: could not persist flags ({type(e).__name__})")
        if not pushed:
            private_print(f"enrichment: STATE PUSH FAILED — the enrich_requested flag for {sent} finding(s) "
                          f"is NOT persisted and the next run WILL re-dispatch them",
                          public="enrichment: STATE PUSH FAILED — flags not persisted")
            rc = 1
    if failed:
        rc = 1
    private_print(f"enrichment: requested {sent}, failed {failed}, still pending {max(0, len(todo) - sent)}",
                  public="enrichment: ok" if not rc else "enrichment: failed")
    return rc


if __name__ == "__main__": sys.exit(main())
