#!/usr/bin/env python3
"""Quiet-log gate: a PUBLIC Actions log must not be a detector readout.

This repo is public, so every Actions log is world-readable. Standing rule: no
public log (and no public artifact) may reveal monitoring status — open findings,
tiers, fire counts, live signal values, shadow signals, which hashes fired,
ack/enrichment state, review backlog — nor identities or handles.

What this proves, offline and against a throwaway copy of the tree:

  1. ACTIONS MODE. The live entry points, in crawl.yml's order (state pull, oracle
     pull, detect, msglog, commands, watchdog, send-pending, heartbeat, state push,
     enrichment) plus weekly.py, run with GITHUB_ACTIONS=true against a fake network
     that makes every noisy path fire: a 650-finding state at its size cap, loud
     findings pending (incident mode), inbound commands, failed enrichment
     dispatches. Their combined stdout+stderr must pass a deny-list: no digit at
     all, no status vocabulary, no hex that could be a hash or id.
  2. LOCAL MODE (self-test of the switch). The same run without GITHUB_ACTIONS
     must still print the detail — otherwise (1) could pass because nothing ran.
  3. STATIC. Every print() in the live modules is either privlog.private_print or a
     constant string, or carries a reviewed `# log-ok:` marker — so a new code path
     the dynamic run does not reach cannot quietly reintroduce a readout.
  4. PUBLIC ARTIFACTS. heartbeat.json (committed, and as run.py writes it) carries
     run-health fields only; the private status/oracle files are gitignored and
     untracked.

Usage:  python3 tests/test_quiet_logs.py      (exit 1 on any failure)
"""
import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = []


def check(name, cond, detail=""):
    # This file's own detail is synthetic, but keep one convention for every gate.
    from notify.privlog import private_print
    RESULTS.append((bool(cond), name))
    private_print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""),
                  public=f"  {'PASS' if cond else 'FAIL'}  {name}")
    return bool(cond)


# ---------------------------------------------------------------- deny-list

DENY = [
    ("a digit (counts, ids, values, times)", re.compile(r"\d")),
    ("status vocabulary", re.compile(
        r"incident|shadow|mindset|unacknowledged|escalat|retired|backlog|queue|"
        r"\bfires?\b|\bfired\b|\btiers?\b|by_tier|\bpending\b|\bpage[sd]?\b|\bdigest\b|"
        r"\backed\b|enrich_requested|requested \w|arrivals|findings?\b|indexed|"
        r"hashed|stale|\bcases?\b|\bloud\b|\bsilent\b", re.I)),
    ("address or hash", re.compile(r"0x[0-9a-f]{4,}|\b[0-9a-f]{8,}\b", re.I)),
    ("handle", re.compile(r"(?<![\w.@/+-])@[A-Za-z]\w{3,}")),
]

# Lines a subprocess may print that are not ours (interpreter noise). Kept tiny.
NOISE = re.compile(r"^\s*$")


def deny_hits(text):
    out = []
    for line in text.splitlines():
        if NOISE.match(line):
            continue
        for name, rx in DENY:
            m = rx.search(line)
            if m:
                out.append((name, line.strip()[:120]))
                break
    return out


# ---------------------------------------------------------------- the throwaway tree

FAKE_NET = r'''
import base64, io, json, os, pathlib, runpy, sys, time, urllib.error, urllib.request
ROOT = pathlib.Path(os.environ["QL_ROOT"])
_mid = [5000]

class _R(io.BytesIO):
    status = 200
    def getcode(self): return self.status
    def __enter__(self): return self
    def __exit__(self, *a): return False

def _json(o):
    return _R(json.dumps(o).encode())

def _err(url, code):
    return urllib.error.HTTPError(url, code, "fake", {}, io.BytesIO(b"{}"))

def fake(req, timeout=None, **kw):
    url = getattr(req, "full_url", str(req))
    method = req.get_method() if hasattr(req, "get_method") else "GET"
    if "api.telegram.org" in url:
        if url.endswith("/getUpdates"):
            chat = int(os.environ["TELEGRAM_CHAT_ID"])
            msg = lambda mid, txt: {"message_id": mid, "date": int(time.time()), "text": txt,
                                    "chat": {"id": chat}, "from": {"id": 4242, "first_name": "t"}}
            return _json({"ok": True, "result": [{"update_id": 71, "message": msg(31, "/status")},
                                                 {"update_id": 72, "message": msg(32, "/cases")}]})
        _mid[0] += 1
        return _json({"ok": True, "result": {"message_id": _mid[0]}})
    if url.endswith("/dispatches"):
        raise _err(url, 500)                 # the failure path that printed a finding id
    if "/commits" in url:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        c = {"commit": {"committer": {"date": now}, "message": "crawl x"}}
        return _json([c] * 30 if "per_page" in url else c)
    if "/contents/" in url:
        path = url.split("/contents/", 1)[1]
        if method == "PUT":
            return _json({"content": {"sha": "fakesha"}})
        src = {"state/alerts-state.json": ROOT / "ql" / "seed-state.json"}.get(path)
        if path.startswith("oracle/"):
            src = ROOT / "ql" / path.split("/", 1)[1]
        if src is not None and src.exists():
            return _json({"content": base64.b64encode(src.read_bytes()).decode(), "sha": "s1"})
        if path == "messages":
            return _json([])
        raise _err(url, 404)
    raise _err(url, 404)                     # nothing leaves this process

urllib.request.urlopen = fake
script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path.insert(0, str(pathlib.Path(script).resolve().parent))
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as e:
    sys.exit(e.code)
'''


def _seed_state():
    """A state at its size cap with every noisy path armed."""
    now = int(time.time())
    open_ = {}
    for i in range(650):                         # > MAX_OPEN: prune retires, some unacked
        tier = ("page", "notify", "digest")[i % 3]
        f = {"id": f"q{i:05d}feedbeef", "key": "0x%040x" % (0xbeef00 + i), "signal": "10",
             "tier": tier, "value": 3.0 + i, "threshold": 1.0, "window": "6h",
             "ts": now - 86400 * 20 - i, "first_ts": f"2026-08-{1 + i % 28:02d}T00:00:00+00:00",
             "type_verified": False, "detail": "synthetic", "headline": ["synthetic"],
             "pending_send": False, "last_sent": "2026-08-01T00:00:00+00:00", "filler": "x" * 900}
        if i < 6:                                # loud and pending: incident mode + header
            f.update(pending_send=True, last_sent=None, tier="page", ts=now - 60,
                     first_ts=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 60)))
        open_[f["id"]] = f
    return {"open": open_, "telegram_offset": 0, "version": 1,
            "retired_notice": {"total": 4, "unacked": 2},
            "detector_health": {"mindset_age_h": 70.0, "mindset_source": "hashed-stale",
                                "open_findings": {"page": 9, "notify": 9, "digest": 9},
                                "fires_last_24h_total": 99}}


def _tree():
    """A copy of the repo with a two-day ledger slice and a synthetic oracle."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="moca-quiet-"))
    ignore = shutil.ignore_patterns(".git", "data", "__pycache__", "incidents", "msglog",
                                    "state.json", ".state_sha", "labels-lite.json",
                                    "watchlist.json", "mindset.json", "balances.json",
                                    "exit_watch.json")
    shutil.copytree(ROOT, d / "repo", ignore=ignore)
    t = d / "repo"
    shutil.copy2(ROOT / "state.json", t / "state.json")        # the CRAWL cursor, public
    (t / "data").mkdir()
    for p in sorted((ROOT / "data").glob("*.jsonl"))[-2:]:
        shutil.copy2(p, t / "data" / p.name)
    q = t / "ql"
    q.mkdir()
    (q / "seed-state.json").write_text(json.dumps(_seed_state()))
    (q / "labels-lite.json").write_text(json.dumps({"entities": {"00deadbeef00cafe": "TP"}}))
    (q / "watchlist.json").write_text(json.dumps({"addresses": ["00deadbeef00cafe"]}))
    (q / "mindset.json").write_text(json.dumps({"generated_at": "2026-01-01T00:00:00Z",
                                                "stale_after_h": 24, "minds": {}}))
    (q / "net.py").write_text(FAKE_NET)
    return d, t


# crawl.yml's order, then weekly.yml. (argv, extra env)
STEPS = [
    (["notify/state_sync.py", "pull"], {}),
    (["notify/state_sync.py", "pull-oracle"], {}),
    (["detect/run.py", "--quiet"], {}),
    (["notify/msglog.py", "pull"], {}),
    (["notify/commands.py"], {}),
    (["notify/watchdog.py"], {}),
    (["notify/telegram.py", "--send-pending"], {}),
    (["notify/telegram.py", "--heartbeat"], {}),
    (["notify/msglog.py", "push"], {}),
    (["notify/state_sync.py", "push"], {}),
    (["notify/request_enrichment.py"], {}),
    (["notify/weekly.py"], {}),
]


def run_pipeline(actions):
    d, t = _tree()
    env = {k: v for k, v in os.environ.items()
           if k not in ("GITHUB_ACTIONS", "THRESHOLDS_JSON", "EXIT_WATCH_JSON")}
    import site
    env.update({"HOME": str(d),                     # never the real ~/.moca-ledger tokens
                "PYTHONUSERBASE": site.getuserbase(),   # ...but keep user-installed deps
                "QL_ROOT": str(t), "PYTHONDONTWRITEBYTECODE": "1",
                "PRIVATE_REPO_PAT": "fake-token-not-a-secret",
                "TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "-100123",
                "TELEGRAM_ACK_USER_IDS": "4242",
                "MINDSET_SALT": "quiet-log-test-salt",
                "SKIP_BALANCE_WATCH": "1", "SKIP_PRICE_FETCH": "1",
                "MPLBACKEND": "Agg"})
    if actions:
        env["GITHUB_ACTIONS"] = "true"
    out, rcs = [], []
    for argv, extra in STEPS:
        e = dict(env, **extra)
        p = subprocess.run([sys.executable, str(t / "ql" / "net.py"), *argv], cwd=t, env=e,
                           capture_output=True, text=True, timeout=600)
        out.append(p.stdout + p.stderr)
        rcs.append((" ".join(argv), p.returncode, (p.stdout + p.stderr)[-400:]))
    return "\n".join(out), rcs, t


# ---------------------------------------------------------------- static scan

LIVE = (["detect/run.py", "detect/balance_watch.py", "detect/price.py", "detect/replay.py",
          "crawl.py", "catalog.py"]
        + sorted(str(p.relative_to(ROOT)) for p in (ROOT / "detect" / "signals").glob("*.py"))
        + sorted(str(p.relative_to(ROOT)) for p in (ROOT / "notify").glob("*.py")
                 if p.name != "privlog.py"))
# crawl.py and catalog.py describe the PUBLIC chain and the public catalog only.
STATIC_EXEMPT = {"crawl.py", "catalog.py"}


def static_hits():
    bad = []
    for rel in LIVE:
        if rel in STATIC_EXEMPT:
            continue
        src = (ROOT / rel).read_text()
        lines = src.splitlines()
        for n in ast.walk(ast.parse(src)):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
                    and not all(isinstance(a, ast.Constant) for a in n.args)
                    and "# log-ok:" not in lines[n.lineno - 1]):
                bad.append(f"{rel}:{n.lineno}")
    return bad


# ---------------------------------------------------------------- main

def main():
    print("quiet logs: public Actions logs carry no detector status ...")

    public, rcs, tree = run_pipeline(actions=True)
    crashed = [f"{a} rc={rc}" for a, rc, _ in rcs if rc not in (0, 1, 3)]
    check("every live entry point ran under the fake network", not crashed, "; ".join(crashed))
    hits = deny_hits(public)
    check("Actions-mode output of the live entry points passes the deny-list", not hits,
          " | ".join(f"{n}: {l}" for n, l in hits[:8]))
    check("Actions mode still says each step ran",
          all(s in public for s in ("detect: ran", "state: pulled", "oracle: restored",
                                    "notify: ok", "commands: ok", "watchdog: ok")),
          public[-400:])
    # detect/run.py actually wrote private health, and the public file stayed lean.
    st = json.loads((tree / "alerts" / "state.json").read_text())
    dh = st.get("detector_health") or {}
    check("detector health lands in the PRIVATE state",
          {"open_findings", "fires_last_24h_total", "shadow_signals", "mindset_source",
           "mindset_age_h", "outflow_x_now"} <= set(dh), ",".join(sorted(dh)))
    sys.path.insert(0, str(ROOT / "detect"))
    import run as detect_run
    allowed = set(detect_run.PUBLIC_HEARTBEAT_FIELDS) | {"note"}
    hb_run = json.loads((tree / "heartbeat.json").read_text())
    check("heartbeat.json as run.py writes it carries run health only",
          set(hb_run) <= allowed, ",".join(sorted(set(hb_run) - allowed)))
    check("run.py no longer writes alerts/state-public.json",
          not (tree / "alerts" / "state-public.json").exists())
    shutil.rmtree(tree.parent, ignore_errors=True)

    local, _, tree = run_pipeline(actions=False)
    shutil.rmtree(tree.parent, ignore_errors=True)
    check("self-test: locally the full detail still prints",
          all(s in local for s in ("detect: rows=", "state: pulled", "UNACKNOWLEDGED",
                                   "incident: header", "shadow: ", "watchdog: ",
                                   "enrichment: dispatch failed for", "commands: ")),
          local[-400:])
    check("self-test: the deny-list does catch the local detail", len(deny_hits(local)) > 5)

    bad = static_hits()
    check("every print() in the live modules is private_print, constant, or reviewed",
          not bad, ", ".join(bad[:12]))

    hb = json.loads((ROOT / "heartbeat.json").read_text())
    check("the committed heartbeat.json carries run health only",
          set(hb) <= allowed, ",".join(sorted(set(hb) - allowed)))
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    gone = ["alerts/state-public.json", "labels/labels-lite.json", "detect/watchlist.json",
            "detect/mindset.json"]
    check("status and oracle files are not tracked", not (set(gone) & set(tracked)),
          ",".join(sorted(set(gone) & set(tracked))))
    ign = (ROOT / ".gitignore").read_text()
    check("status and oracle files are gitignored (git add -A cannot republish them)",
          all(g in ign for g in gone))

    bad = [n for ok, n in RESULTS if not ok]
    print(f"quiet logs: {'FAIL' if bad else 'OK'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
