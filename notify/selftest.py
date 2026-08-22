#!/usr/bin/env python3
"""Daily end-to-end self-test of the alerting path.

The failure this exists to catch is the silent one: the workflow stays green, the
channel stays quiet, and nobody learns the bot stopped working until an incident
needs it. Quiet is indistinguishable from dead unless something exercises the path
on purpose.

Four legs, each verified against a real result rather than an absence of exceptions:

  1. RENDER    — the plain-English layer turns a finding into a message, and the
                 message carries no name, email, IP or hash. A silent fall back to
                 the terse renderer counts as a failure.
  2. DELIVER   — the real send path (notify/telegram.send_pending) runs against an
                 ISOLATED state file and Telegram's own API response is checked for
                 ok:true and a message_id. Real findings are never touched.
  3. DISPATCH  — a repository_dispatch to the private side is accepted (HTTP 204).
  4. CLEAN     — the synthetic message is deleted, the isolated state is thrown
                 away, and the REAL state is swept for any synthetic leftovers
                 from an earlier run that died halfway.

The synthetic entity is `SELFTEST-NOT-A-WALLET` / id `selftest-synthetic`: not a
hex address, so it can never collide with a real wallet, can never be prefix-matched
by /close, and is caught by the sweep by its `selftest` prefix.

The bot takes no action on the platform here, and nothing about this is an alert:
the delivered text says SELF-TEST in its first three words and deletes itself.

Usage:
    python3 notify/selftest.py            # run all legs
    python3 notify/selftest.py --sweep    # cleanup only (safe to run any time)
"""
import argparse, json, os, pathlib, re, shutil, sys, tempfile, urllib.parse, urllib.request
import datetime as dt

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SYNTH_ID  = "selftest-synthetic"
SYNTH_KEY = "SELFTEST-NOT-A-WALLET"
SYNTH_PREFIX = "selftest"

BANNER = ("\U0001f9ea <b>SELF-TEST — not an alert.</b>\n"
          "Automatic daily check that this channel still works. "
          "Nothing happened on the platform; no case changed. "
          "This message deletes itself a second from now.")

legs = []


def leg(name, ok, detail=""):
    legs.append((bool(ok), name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def synthetic_finding():
    now = dt.datetime.now(dt.UTC)
    return {
        "id": SYNTH_ID, "key": SYNTH_KEY, "signal": "10", "tier": "notify",
        "value": 0.51, "threshold": 0.45, "organic_p95": 0.31, "window": "6h",
        "ts": int(now.timestamp()), "first_ts": now.isoformat(),
        "as_of_block": 0, "type_verified": False, "backfill": False,
        "detail": "synthetic self-test finding — no real entity, no real payouts",
        "headline": ["synthetic self-test finding"],
        "evidence": {"n": 113, "top1": 0.51, "hours": 6},
        "recommended_action": "nothing — this is the daily self-test",
        "owner": "self-test", "pending_send": True,
    }


# ------------------------------------------------------------------ leg 1

def _pii_rules():
    """Borrow the repo's own PII rules rather than writing a second, weaker copy."""
    argv = sys.argv[:]
    try:
        sys.argv = ["test_pii", "--tree", str(ROOT)]
        sys.path.insert(0, str(ROOT / "tests"))
        import test_pii
        return test_pii.DENY, test_pii.ALLOW_LINE
    finally:
        sys.argv = argv


def leg_render():
    from notify import telegram
    f = synthetic_finding()
    try:
        from notify.explain import humanise
    except Exception as e:
        return leg("RENDER  plain-English layer imports", False, f"{type(e).__name__}: {e}")
    try:
        human = humanise(dict(f))
    except Exception as e:
        return leg("RENDER  humanise() runs on a finding", False, f"{type(e).__name__}: {e}")
    rendered = telegram.render(dict(f))
    ok = leg("RENDER  humanise() produces a message", bool(human and human.strip()),
             f"{len(human or '')} chars")
    # render() swallows a humanise() exception and quietly returns the terse form.
    # Equal output is the only proof the plain-English layer actually ran.
    ok &= leg("RENDER  no silent fallback to the terse renderer", rendered == human)
    ok &= leg("RENDER  message fits one Telegram message", len(rendered) <= 4096,
              f"{len(rendered)} chars")
    try:
        deny, allow = _pii_rules()
        hits = [(n, m.group(0)[:40]) for ln in rendered.splitlines() if not allow.search(ln)
                for n, rx in deny for m in [rx.search(ln)] if m]
        hits += [("wallet address", a) for a in re.findall(r"0x[0-9a-f]{40}", rendered, re.I)]
        ok &= leg("RENDER  rendered message carries no PII", not hits, str(hits[:3]))
    except Exception as e:
        ok &= leg("RENDER  PII rules loaded", False, f"{type(e).__name__}: {e}")
    return ok


# ------------------------------------------------------------------ leg 2

def leg_deliver(tmpdir):
    """Run the REAL send path against an isolated state file.

    telegram.render is swapped for the self-test banner for the duration: the state
    machine (write-before-send, send_ok, tg_message_id, by_message) is what is under
    test, and a message shaped like a real alert must never reach the channel."""
    from notify import telegram
    tmp = tmpdir / "state.json"
    f = synthetic_finding()
    tmp.write_text(json.dumps({"open": {SYNTH_KEY: f}, "sent": {},
                               "telegram_offset": 0, "version": 1}, indent=1))
    o_state, o_render, o_send = telegram.STATE, telegram.render, telegram.send
    try:
        telegram.STATE = tmp
        telegram.render = lambda _f: BANNER
        # The workflow header says this job "posts nothing to the channel on success".
        # A `notify`-tier finding outside an incident sends LOUD, so every phone in the
        # group pinged daily — and the banner explaining it was deleted a second later,
        # leaving a notification for a message that no longer exists. The real send path
        # is still exercised; only the notification is suppressed.
        telegram.send = lambda text, photo=None, silent=False: o_send(text, photo=photo, silent=True)
        rc = telegram.send_pending()
    finally:
        telegram.STATE, telegram.render, telegram.send = o_state, o_render, o_send

    s = json.loads(tmp.read_text())
    sent = (s.get("open") or {}).get(SYNTH_KEY) or {}
    mid = sent.get("tg_message_id")
    ok = leg("DELIVER send_pending() exits clean", rc == 0, f"rc={rc}")
    ok &= leg("DELIVER Telegram accepted the message (ok:true from the API)",
              sent.get("send_ok") is True, sent.get("send_error") or "")
    ok &= leg("DELIVER Telegram returned a message_id", bool(mid), f"message_id={mid}")
    ok &= leg("DELIVER the finding was cleared from pending", not sent.get("pending_send"))
    ok &= leg("DELIVER the reply -> case map was written",
              (s.get("by_message") or {}).get(str(mid)) == SYNTH_ID,
              f"by_message={s.get('by_message')}")
    return ok, mid


# ------------------------------------------------------------------ leg 3

def leg_dispatch():
    from notify.request_enrichment import dispatch
    from notify.state_sync import _pat
    pat = _pat()
    if not pat:
        return leg("DISPATCH tier-2 enrichment accepted", False,
                   "PRIVATE_REPO_PAT is not set — the tier-2 leg cannot be tested")
    ok, detail = dispatch(SYNTH_ID, pat)
    return leg("DISPATCH tier-2 enrichment accepted by the private repo", ok, detail)


# ------------------------------------------------------------------ leg 4

def delete_message(mid):
    if not mid: return True
    from notify import telegram
    r = telegram._post("deleteMessage", {"chat_id": telegram.CHAT(), "message_id": mid})
    if r.get("ok"):
        return True
    # Do not leave a message the channel cannot explain.
    telegram.send("\U0001f9ea The message above was the daily self-test, not an alert. "
                  "I could not delete it (" + str(r.get("error"))[:60] + ").", silent=True)
    return False


def sweep():
    """Remove any synthetic finding from the REAL state. Idempotent; safe alone.

    Pulls first so the copy being edited is the current one: a crawl run may have
    written state since this job started, and pushing a stale copy would delete
    real findings to clean up a fake one."""
    from notify import state_sync
    if not state_sync.pull():
        print("  sweep: could not restore state — cannot verify the state is clean")
        return False
    st_path = state_sync.STATE
    if not st_path.exists():
        print("  sweep: no state file"); return True
    s = json.loads(st_path.read_text())
    open_f = s.get("open") or {}
    doomed = [k for k, f in open_f.items()
              if str(k).lower().startswith(SYNTH_PREFIX) or k == SYNTH_KEY
              or str(f.get("id", "")).lower().startswith(SYNTH_PREFIX)]
    bm = s.get("by_message") or {}
    doomed_msgs = [m for m, fid in bm.items() if str(fid).lower().startswith(SYNTH_PREFIX)]
    if not doomed and not doomed_msgs:
        print("  sweep: no synthetic state to remove"); return True
    for k in doomed: open_f.pop(k, None)
    for m in doomed_msgs: bm.pop(m, None)
    st_path.write_text(json.dumps(s, indent=1))
    print(f"  sweep: removed {len(doomed)} synthetic finding(s) and {len(doomed_msgs)} message map entry(s)")
    if not state_sync.push():
        print("  sweep: THE REMOVAL WAS NOT PERSISTED — synthetic state is still in the private repo")
        return False
    return True


# ------------------------------------------------------------------ report

def _owner():
    try:
        return json.loads((ROOT / "detect" / "thresholds.json").read_text()).get(
            "escalation_owner", "the on-call")
    except Exception:
        return "the on-call"


def report(ok):
    lines = ["# Daily alerting self-test", "",
             f"`{dt.datetime.now(dt.UTC).strftime('%Y-%m-%d %H:%M')} UTC` — "
             f"**{'all legs green' if ok else 'FAILED'}**", ""]
    for good, name, detail in legs:
        lines.append(f"- {'✅' if good else '❌'} `{name}`" + (f" — {detail}" if detail else ""))
    body = "\n".join(lines)
    print("\n" + body)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh: fh.write(body + "\n")
    if not ok:
        # Loud, because a broken alerting path cannot announce itself later.
        failed = [n for good, n, _ in legs if not good]
        try:
            from notify import telegram
            # Written for whoever reads it at 09:23, not for whoever wrote the legs.
            plain = {"RENDER": "writing an alert in plain English",
                     "DELIVER": "sending a message to this channel",
                     "DISPATCH": "asking for the follow-up account detail",
                     "CLEAN": "cleaning up after itself",
                     "RUN": "running at all"}
            parts = sorted({plain.get(n.split()[0], n.split()[0]) for n in failed})
            telegram.send(
                "\U0001f534 <b>My daily check on myself failed.</b>\n"
                f"What did not work: <b>{'; '.join(parts) or 'unknown'}</b>\n\n"
                "This is the check that proves alerts can still reach this channel. Until it "
                "passes, assume I may be watching the chain without being able to tell anyone "
                "\u2014 so treat quiet as unknown, not as safe.\n"
                f"Please tell <b>{_owner()}</b>."
                + (f"\nRun log: {os.environ['RUN_URL']}" if os.environ.get("RUN_URL") else ""),
                silent=False)
        except Exception as e:
            print(f"selftest: could not post the failure notice ({type(e).__name__})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true", help="cleanup only")
    a = ap.parse_args()
    if a.sweep:
        return 0 if sweep() else 1

    print("selftest: exercising the alerting path with a synthetic finding ...")
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="moca-selftest-"))
    mid = None
    ok = True
    try:
        ok &= leg_render()
        delivered, mid = leg_deliver(tmpdir)
        ok &= delivered
        ok &= leg_dispatch()
    except Exception as e:
        ok = leg("RUN     self-test completed without crashing", False, f"{type(e).__name__}: {e}")
    finally:
        # Cleanup is not conditional on success: a half-failed run is exactly when
        # synthetic state gets left behind.
        cleaned = delete_message(mid)
        shutil.rmtree(tmpdir, ignore_errors=True)
        swept = sweep()
        ok &= leg("CLEAN   synthetic message removed from the channel", cleaned)
        ok &= leg("CLEAN   no synthetic finding left in the state", swept)
    report(ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
