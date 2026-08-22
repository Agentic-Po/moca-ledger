#!/usr/bin/env python3
"""Pipeline-safety tests for the detector's operational state.   # pii-ok

Two failures this guards against, both of which take the detector dark during the
incident it exists for:

  1. `alerts/state.json` grows past 1 MB. The GitHub Contents API then stops
     returning inline base64 `content`, and the restore step — which has no
     `|| true` — fails the whole run. Covered: prune() must bring a 1.5 MB state
     back under the cap, and the restore must survive a `content: null` response.

  2. `enrich_requested` is not persisted, so every run re-dispatches the same
     findings forever. Covered: two consecutive runs against one state file must
     dispatch once, not twice.

Usage:  python3 tests/test_state.py        (exit 1 on any failure)
"""
import base64, io, json, os, pathlib, sys, tempfile, urllib.error, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notify import state_sync                      # noqa: E402
from notify import request_enrichment as enrich    # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


# ---------------------------------------------------------------- fixtures

def finding(i, settled=False, fat=0):
    """A finding shaped like the real ones (~900 B serialised)."""
    f = {
        "id": f"f{i:08x}", "key": f"synthetic-entity-{i:06d}", "signal": str(10 + i % 8),
        "tier": ("page" if i % 20 == 0 else "notify" if i % 5 == 0 else "digest"),
        "value": 0.4212 + (i % 100) / 1000, "threshold": 0.45, "organic_p95": 0.31,
        "window": "6h", "ts": 1755900000 + i, "first_ts": f"2026-08-{1 + i % 21:02d}T0{i % 10}:00:00+00:00",
        "as_of_block": 34512099 + i, "backfill": False, "type_verified": False,
        "unit_source": "frozen", "mindset_source": "posthog", "episode_fires": i % 30,
        "episode_first": "2026-08-19T13:10:00+00:00", "episode_last": "2026-08-21T01:30:00+00:00",
        "owner": "Po (interim)", "escalation": "review before the next payout batch",
        "detail": "one creator took an outsized share of reward payouts in the window " * 2,
        "headline": ["top1 share above the level that triggers a look",
                     "n above the minimum for this signal",
                     "same entity seen in an earlier window"],
        "evidence": {"n": 113 + i, "top1": 0.62, "top3": 0.71, "hours": 6},
        "recommended_action": "confirm the payouts are real rewards before drawing any conclusion",
        "view_png": f"alerts/views/{i:06d}.png", "pending_send": False,
    }
    if settled:
        f["ack_by"] = "go-live-seed"; f["ack_ts"] = "2026-08-22T09:00:00+00:00"
    if fat:
        f["padding"] = "x" * fat
    return f


def synthetic_state(n, settled_from=0, fat=0):
    open_f = {}
    for i in range(n):
        open_f[f"synthetic-entity-{i:06d}"] = finding(i, settled=(i >= settled_from), fat=fat)
    return {"open": open_f, "sent": {}, "telegram_offset": 238013499, "version": 1,
            "by_message": {str(90000 + i): f"f{i:08x}" for i in range(400)}}


# ---------------------------------------------------------------- 1. size

def test_prune_brings_a_1_5mb_state_under_the_cap():
    st = synthetic_state(1700, settled_from=100)
    before = state_sync._size(st)
    check("fixture really is >1.5 MB", before > 1_500_000, f"{before} bytes")
    st = state_sync.prune(st)
    after = state_sync._size(st)
    check("prune() brings a 1.5 MB state under the 1 MB API ceiling",
          after < 1_000_000, f"{before} -> {after} bytes, {len(st['open'])} findings kept")
    check("prune() respects the byte budget it declares", after <= state_sync.MAX_BYTES,
          f"{after} <= {state_sync.MAX_BYTES}")
    check("nothing is dropped without being counted",
          st.get("retired") == 1700 - len(st["open"]), f"retired={st.get('retired')}")
    check("by_message is trimmed to KEEP_MSGS",
          len(st["by_message"]) == state_sync.KEEP_MSGS, f"400 -> {len(st['by_message'])}")


def test_byte_budget_engages_when_the_count_limit_is_not_enough():
    """MAX_OPEN alone guarantees nothing: 600 fat findings still blow the cap.
    This is the regression guard for 'every field you add multiplies by ~430'."""
    st = synthetic_state(state_sync.MAX_OPEN, settled_from=50, fat=1800)
    before = state_sync._size(st)
    check("600 fat findings exceed the cap on their own", before > 1_000_000, f"{before} bytes")
    st = state_sync.prune(st)
    after = state_sync._size(st)
    check("byte budget trims below MAX_OPEN when findings get fat",
          after <= state_sync.MAX_BYTES and len(st["open"]) < state_sync.MAX_OPEN,
          f"{before} -> {after} bytes, {len(st['open'])} kept")


def test_prune_drops_settled_findings_before_live_ones():
    st = synthetic_state(1000, settled_from=200)      # 0..199 live, 200..999 settled
    st = state_sync.prune(st)
    kept = st["open"]
    live_kept = sum(1 for f in kept.values() if not state_sync._settled(f))
    check("every unacknowledged finding survives the prune", live_kept == 200,
          f"{live_kept}/200 live kept, {len(kept)} total")


def test_prune_is_a_no_op_below_the_limits():
    st = synthetic_state(120, settled_from=60)
    st["by_message"] = dict(list(st["by_message"].items())[:20])   # under KEEP_MSGS too
    snapshot = json.dumps(st, indent=1)
    st2 = state_sync.prune(st)
    check("a small state is returned byte-identical (no key churn, no retired counter)",
          json.dumps(st2, indent=1) == snapshot)


def test_live_state_fits_the_budget_at_max_open():
    """Runs against the real state when it is on disk (after a pull). If the average
    finding grows enough that MAX_OPEN of them would breach the cap, fail here rather
    than during an incident."""
    live = ROOT / "alerts" / "state.json"
    if not live.exists():
        print("  SKIP  live-state budget check (no alerts/state.json on disk)"); return
    s = json.loads(live.read_text())
    n = len(s.get("open") or {})
    if not n:
        print("  SKIP  live-state budget check (no open findings)"); return
    avg = live.stat().st_size / n
    projected = avg * state_sync.MAX_OPEN
    check("MAX_OPEN findings at today's average size still fit the byte budget",
          projected < state_sync.MAX_BYTES,
          f"{n} findings, {avg:.0f} B each -> {projected:.0f} B projected at MAX_OPEN={state_sync.MAX_OPEN}")


# ---------------------------------------------------------------- 2. restore

class _FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _with_fake_api(get_result, download_bytes=None, raises=None):
    """Patch state_sync's token, API call and raw fetch; return (ok, restored_bytes)."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    o_state, o_pat, o_req, o_open = (state_sync.STATE, state_sync._pat,
                                     state_sync._req, urllib.request.urlopen)
    calls = {"download": 0}
    try:
        state_sync.STATE = tmp
        state_sync._pat = lambda: "fake-token-not-a-secret"

        def fake_req(method, body=None):
            if raises: raise raises
            return get_result
        state_sync._req = fake_req

        def fake_open(req, timeout=None):
            calls["download"] += 1
            return _FakeResp(download_bytes or b"")
        urllib.request.urlopen = fake_open

        ok = state_sync.pull()
        return ok, (tmp.read_bytes() if tmp.exists() else None), calls
    finally:
        state_sync.STATE, state_sync._pat = o_state, o_pat
        state_sync._req, urllib.request.urlopen = o_req, o_open


def test_restore_handles_content_null():
    payload = json.dumps({"open": {"a": {"id": "a"}, "b": {"id": "b"}}}).encode()
    ok, restored, calls = _with_fake_api(
        {"content": None, "download_url": "https://example.invalid/raw", "sha": "deadbeef"},
        download_bytes=payload)
    check("restore succeeds when the API returns content: null", ok)
    check("restore used the download_url fallback exactly once", calls["download"] == 1)
    check("restored bytes are the real state", restored == payload,
          f"{len(restored or b'')} bytes")


def test_restore_handles_content_empty_string():
    """GitHub returns content:'' with encoding:'none' for large files, not null."""
    payload = json.dumps({"open": {"a": {"id": "a"}}}).encode()
    ok, restored, calls = _with_fake_api(
        {"content": "", "encoding": "none", "download_url": "https://example.invalid/raw", "sha": "s"},
        download_bytes=payload)
    check("restore succeeds when the API returns content: '' (encoding none)", ok)
    check("restored bytes are the real state (empty-string case)", restored == payload)


def test_restore_fails_closed_with_no_content_and_no_url():
    ok, restored, _ = _with_fake_api({"content": None, "sha": "s"})
    check("no content and no download_url fails CLOSED", ok is False)
    check("nothing is written when the restore fails", restored is None)


def test_restore_fails_closed_on_unparseable_body():
    ok, restored, _ = _with_fake_api(
        {"content": None, "download_url": "https://example.invalid/raw", "sha": "s"},
        download_bytes=b"<html>rate limited</html>")
    check("a non-JSON download fails CLOSED instead of overwriting state", ok is False)
    check("nothing is written when the download is not JSON", restored is None)


def test_restore_fails_closed_on_http_500_but_not_on_404():
    err5 = urllib.error.HTTPError("u", 500, "boom", {}, None)
    ok, _, _ = _with_fake_api(None, raises=err5)
    check("HTTP 500 on restore fails CLOSED", ok is False)
    err4 = urllib.error.HTTPError("u", 404, "missing", {}, None)
    ok, _, _ = _with_fake_api(None, raises=err4)
    check("HTTP 404 (first run, nothing stored yet) is not a failure", ok is True)


# ---------------------------------------------------------------- 3. enrichment

def test_no_double_dispatch_across_two_runs():
    """Simulate two consecutive crawl runs against one state file."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    st = {"open": {}, "version": 1}
    for i in range(3):
        f = finding(i)
        f.update({"tier": "page", "pending_send": False, "id": f"case{i}"})
        f.pop("ack_by", None)
        st["open"][f["key"]] = f
    tmp.write_text(json.dumps(st, indent=1))

    dispatched, o_state, o_dispatch, o_env = [], enrich.STATE, enrich.dispatch, os.environ.get("PRIVATE_REPO_PAT")
    import notify.state_sync as ss
    o_push = ss.push
    try:
        enrich.STATE = tmp
        enrich.dispatch = lambda fid, pat, target=None: (dispatched.append(fid), (True, "http 204"))[1]
        ss.push = lambda: True                       # pretend the private repo accepted it
        os.environ["PRIVATE_REPO_PAT"] = "fake-token-not-a-secret"

        rc1 = enrich.main(); after_run1 = len(dispatched)
        rc2 = enrich.main(); after_run2 = len(dispatched)
    finally:
        enrich.STATE, enrich.dispatch, ss.push = o_state, o_dispatch, o_push
        if o_env is None: os.environ.pop("PRIVATE_REPO_PAT", None)
        else: os.environ["PRIVATE_REPO_PAT"] = o_env

    check("run 1 dispatches every eligible finding once", after_run1 == 3, f"{after_run1} dispatches")
    check("run 2 dispatches nothing (no double-dispatch)", after_run2 == after_run1,
          f"{after_run2 - after_run1} extra dispatches on the second run")
    check("both runs exit clean", rc1 == 0 and rc2 == 0, f"rc={rc1},{rc2}")
    flags = [f.get("enrich_requested") for f in json.loads(tmp.read_text())["open"].values()]
    check("the flag is written to the state file", all(flags), f"{sum(1 for x in flags if x)}/3 flagged")


def test_lost_push_is_reported_not_swallowed():
    """If the state push fails the flag does not survive, so the next run WILL
    re-dispatch. That must be a non-zero exit, not a silent success."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    f = finding(1); f.update({"tier": "page", "pending_send": False, "id": "case1"}); f.pop("ack_by", None)
    tmp.write_text(json.dumps({"open": {f["key"]: f}, "version": 1}, indent=1))

    import notify.state_sync as ss
    o_state, o_dispatch, o_push, o_env = enrich.STATE, enrich.dispatch, ss.push, os.environ.get("PRIVATE_REPO_PAT")
    try:
        enrich.STATE = tmp
        enrich.dispatch = lambda fid, pat, target=None: (True, "http 204")
        ss.push = lambda: False                      # the push is dropped
        os.environ["PRIVATE_REPO_PAT"] = "fake-token-not-a-secret"
        rc = enrich.main()
    finally:
        enrich.STATE, enrich.dispatch, ss.push = o_state, o_dispatch, o_push
        if o_env is None: os.environ.pop("PRIVATE_REPO_PAT", None)
        else: os.environ["PRIVATE_REPO_PAT"] = o_env
    check("a dropped state push exits non-zero instead of pretending it worked", rc == 1, f"rc={rc}")


def test_rejected_dispatch_is_not_marked_requested():
    tmp = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    f = finding(1); f.update({"tier": "page", "pending_send": False, "id": "case1"}); f.pop("ack_by", None)
    tmp.write_text(json.dumps({"open": {f["key"]: f}, "version": 1}, indent=1))
    import notify.state_sync as ss
    o_state, o_dispatch, o_push, o_env = enrich.STATE, enrich.dispatch, ss.push, os.environ.get("PRIVATE_REPO_PAT")
    try:
        enrich.STATE = tmp
        enrich.dispatch = lambda fid, pat, target=None: (False, "HTTPError: 403")
        ss.push = lambda: True
        os.environ["PRIVATE_REPO_PAT"] = "fake-token-not-a-secret"
        rc = enrich.main()
    finally:
        enrich.STATE, enrich.dispatch, ss.push = o_state, o_dispatch, o_push
        if o_env is None: os.environ.pop("PRIVATE_REPO_PAT", None)
        else: os.environ["PRIVATE_REPO_PAT"] = o_env
    still = json.loads(tmp.read_text())["open"][f["key"]].get("enrich_requested")
    check("a rejected dispatch is not marked requested (it will be retried)", not still)
    check("a rejected dispatch exits non-zero", rc == 1, f"rc={rc}")


# ---------------------------------------------------------------- run

def main():
    print("state: pipeline-safety checks ...")
    for fn in (test_prune_brings_a_1_5mb_state_under_the_cap,
               test_byte_budget_engages_when_the_count_limit_is_not_enough,
               test_prune_drops_settled_findings_before_live_ones,
               test_prune_is_a_no_op_below_the_limits,
               test_live_state_fits_the_budget_at_max_open,
               test_restore_handles_content_null,
               test_restore_handles_content_empty_string,
               test_restore_fails_closed_with_no_content_and_no_url,
               test_restore_fails_closed_on_unparseable_body,
               test_restore_fails_closed_on_http_500_but_not_on_404,
               test_no_double_dispatch_across_two_runs,
               test_lost_push_is_reported_not_swallowed,
               test_rejected_dispatch_is_not_marked_requested):
        fn()
    bad = [r for r in RESULTS if not r[0]]
    if bad:
        print(f"state: FAIL — {len(bad)} of {len(RESULTS)} checks failed")
        return 1
    print(f"state: OK — all {len(RESULTS)} checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
