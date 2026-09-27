"""Health as the bot sees it: public run health + private detector health.

Until 2026-09 heartbeat.json carried both, and it is committed to a PUBLIC repo on
every run — so open findings by tier, fires in the last 24 h, the outflow multiple,
the shadow list and the mind-set age were a live readout for whoever is being
watched. The split now is:

  * heartbeat.json (public)            run_ts, crawl_ok, rows_total, ledger_last,
                                       lag_blocks — is the crawl landing?
  * state["detector_health"] (private) detect_ok, mindset_age_h, mindset_source,
                                       open_findings, fires_last_24h_total,
                                       shadow_signals, shadow_refused,
                                       thresholds_override_error, outflow_x_now

The private half is written by detect/run.py into alerts/state.json, which
notify/state_sync.py already carries to and from Agentic-Po/moca-ledger-private —
no extra push, no extra secret. Readers (telegram.py --heartbeat, commands.py
/status, watchdog.py, weekly.py) call load() and see one dict, as before.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HEARTBEAT = ROOT / "heartbeat.json"
STATE = ROOT / "alerts" / "state.json"

PRIVATE_KEY = "detector_health"


def _read(p):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return {}


def load(state=None, root=None):
    """Public heartbeat overlaid with the private detector health.

    `state` is the already-loaded private state when the caller has it; otherwise
    alerts/state.json is read (absent on a public checkout -> run health only).
    The detector half wins on a clash: its run_ts is the detector's own."""
    base = pathlib.Path(root) if root else ROOT
    hb = _read(base / "heartbeat.json")
    if state is None:
        state = _read(base / "alerts" / "state.json")
    hb.update((state or {}).get(PRIVATE_KEY) or {})
    return hb
