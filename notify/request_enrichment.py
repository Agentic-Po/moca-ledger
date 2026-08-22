#!/usr/bin/env python3
"""Ask the private side to tier-2 enrich any finding that has been alerted but
not yet enriched. Sends a repository_dispatch; no-op without PRIVATE_REPO_PAT
(the private side also polls hourly as a safety net)."""
import json, os, pathlib, sys, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "alerts" / "state.json"
TARGET = "Agentic-Po/moca-ledger-private"

def main():
    pat = os.environ.get("PRIVATE_REPO_PAT")
    s = json.loads(STATE.read_text()) if STATE.exists() else {}
    todo = [f for f in (s.get("open") or {}).values()
            if f.get("tier") in ("page", "notify") and not f.get("pending_send")
            and f.get("ack_by") != "go-live-seed" and not f.get("enrich_requested") and f.get("id")]
    if not todo:
        print("enrichment: nothing to request"); return 0
    if not pat:
        print(f"enrichment: {len(todo)} pending, no PAT — private side will pick them up on its hourly pass"); return 0
    sent = 0
    for f in todo[:10]:
        body = json.dumps({"event_type": "finding", "client_payload": {"finding_id": f["id"]}}).encode()
        req = urllib.request.Request(f"https://api.github.com/repos/{TARGET}/dispatches", data=body,
                                     headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json",
                                              "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20); f["enrich_requested"] = True; sent += 1
        except Exception as e:
            print("enrichment: dispatch failed:", str(e)[:60])
    STATE.write_text(json.dumps(s, indent=1))
    print(f"enrichment: requested {sent}")
    return 0

if __name__ == "__main__": sys.exit(main())
