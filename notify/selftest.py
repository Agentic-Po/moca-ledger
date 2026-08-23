#!/usr/bin/env python3
"""Daily end-to-end self-test of the alerting path.

The failure this exists to catch is the silent one: the workflow stays green, the
channel stays quiet, and nobody learns the bot stopped working until an incident
needs it. Quiet is indistinguishable from dead unless something exercises the path
on purpose.

Five legs, each verified against a real result rather than an absence of exceptions:

  1. RENDER    — the plain-English layer turns a finding into a message, and the
                 message carries no name, email, IP or hash. A silent fall back to
                 the terse renderer counts as a failure. Both tiers that reach a
                 person are rendered: `notify`, and `page` — which must arrive as a
                 page and must still carry its escalation line.
  2. DELIVER   — the real send path (notify/telegram.send_pending) runs against an
                 ISOLATED state file and Telegram's own API response is checked for
                 ok:true and a message_id. Real findings are never touched.
  3. PAGE      — the same path at the tier that matters at 03:00. A chart is drawn by
                 the real renderer and the twin photo+text path is exercised against
                 the live API: text message, chart posted under it as a reply, both
                 ids mapped back to the case. Sound is the one property that cannot
                 be proven live — proving it means pinging every phone in the group
                 nightly, which is how a channel gets muted — so what is checked is
                 that the send path ASKED for sound and that this run refused it.
  4. DISPATCH  — a repository_dispatch to the private side is accepted (HTTP 204).
  5. CLEAN     — every synthetic message is deleted, the isolated state is thrown
                 away, the REAL state file is shown byte-for-byte unwritten by this
                 run, and it is swept for synthetic leftovers from an earlier run
                 that died halfway.

Incident mode is deliberately NOT exercised live, and the reason is not squeamishness.
`telegram._incident_header()` is built inside send_pending() and never passes through
telegram.render, so the banner substitution that keeps every other message in this file
honest cannot reach it: a live incident leg would post "Incident mode — N new alert(s)
this run" into the real channel, and deleteMessage recalls nothing already forwarded or
screenshotted. Worse, send_pending() writes `incident` into whatever telegram.STATE
points at, and an incident object left in the live state file makes the channel send six
hours of REAL alerts silently, under a header nobody received. Incident mode is proven
offline instead, by tests/test_notify.py:t_incident — which runs in this same workflow
job one step earlier, against a stubbed sender. What this file adds is the guard:
_isolation_ok() before every send, and a byte comparison of the live state after.

The synthetic entity is `SELFTEST-NOT-A-WALLET` / id `selftest-synthetic` (the page leg
uses the `-page` variants of both): not a hex address, so it can never collide with a
real wallet, can never be prefix-matched by /close, and is caught by the sweep by its
`selftest` prefix.

The bot takes no action on the platform here, and nothing about this is an alert:
the delivered text says SELF-TEST in its first three words and deletes itself.

Usage:
    python3 notify/selftest.py            # run all legs
    python3 notify/selftest.py --sweep    # cleanup only (safe to run any time)
"""
import argparse, contextlib, json, os, pathlib, re, shutil, sys, tempfile, urllib.parse, urllib.request
import datetime as dt

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SYNTH_ID  = "selftest-synthetic"
SYNTH_KEY = "SELFTEST-NOT-A-WALLET"
SYNTH_PAGE_ID  = "selftest-synthetic-page"
SYNTH_PAGE_KEY = "SELFTEST-NOT-A-WALLET-PAGE"
SYNTH_PREFIX = "selftest"

BANNER = ("\U0001f9ea <b>SELF-TEST — not an alert.</b>\n"
          "Automatic daily check that this channel still works. "
          "Nothing happened on the platform; no case changed. "
          "This message deletes itself a second from now.")

# The page banner has a requirement the notify one does not: it must be LONGER than a
# photo caption. At or below telegram.CAPTION_MAX the sender puts the chart in the
# caption and the twin photo+text path — the branch only the page tier takes, and the
# only one that posts two messages for one finding — never executes. A check in
# tests/test_notify.py asserts the length, so trimming this copy fails there instead of
# quietly downgrading the daily check to the single-message path leg 2 already covers.
PAGE_BANNER = (
    "\U0001f9ea <b>SELF-TEST — not an alert. Nothing has happened on the platform.</b>\n"
    "Automatic daily check that the loudest tier of this channel still works end to end: "
    "the message, the chart under it, and the record of both. No wallet moved, no case "
    "changed, and nothing was paused or blocked — this bot only ever informs.\n\n"
    "<b>The ping was suppressed on purpose.</b> A real page makes a sound. This one must "
    "not wake anyone, so the send path was asked for sound and answered silently. That it "
    "asked is the thing being checked.\n\n"
    "<b>The chart below is invented.</b> It is drawn from a finding that does not exist, "
    "with numbers that measure nothing. It is not a picture of anything on the platform.\n\n"
    "<b>Why this message is long.</b> Above 1024 characters the page tier stops putting "
    "its chart in the caption and posts it as a second message replying to the first. That "
    "twin path is what this check exists to exercise, and a short message cannot.\n\n"
    "Both messages delete themselves a second from now. If you are reading this more than "
    "a minute after it arrived, the deletion failed — the check says so in its own "
    "message, so treat this one as spent rather than as news.\n\n"
    "Silence in this channel is never an all-clear. Only a person says that.")

legs = []


def leg(name, ok, detail=""):
    legs.append((bool(ok), name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def _escalation_owner():
    """Whatever detect/run.py would stamp on a real finding as `owner`.

    Read from the deployed thresholds file rather than hard-coded, and mirroring
    run.py's own `or "UNASSIGNED"` fallback, so the render leg compares the page
    against what a real page would actually say.

    It deliberately does NOT judge whether the name is a real person. Naming one is an
    open item owned outside this repo (HANDOFF §6.6 — still "Po (interim)"), and a
    leg that failed on it would turn the daily check red every morning for a reason
    nobody here can fix. A self-test that is red every day is a self-test nobody reads.
    What the leg checks is that the LINE is still there, which is the part that can
    disappear silently."""
    try:
        v = json.loads((ROOT / "detect" / "thresholds.json").read_text()).get("escalation_owner")
    except Exception as e:
        print(f"selftest: could not read escalation_owner ({type(e).__name__}: {e})", file=sys.stderr)
        return "UNASSIGNED"
    return str(v or "").strip() or "UNASSIGNED"


def synthetic_finding(tier="notify"):
    """The finding every leg is built on. `tier` picks which alerting tier is exercised."""
    now = dt.datetime.now(dt.UTC)
    page = tier == "page"
    return {
        "id": SYNTH_PAGE_ID if page else SYNTH_ID,
        "key": SYNTH_PAGE_KEY if page else SYNTH_KEY,
        "signal": "10", "tier": tier,
        "value": 0.51, "threshold": 0.45, "organic_p95": 0.31, "window": "6h",
        "ts": int(now.timestamp()), "first_ts": now.isoformat(),
        "as_of_block": 0, "type_verified": False, "backfill": False,
        "detail": "synthetic self-test finding — no real entity, no real payouts",
        "headline": ["synthetic self-test finding"],
        "evidence": {"n": 113, "top1": 0.51, "hours": 6},
        "recommended_action": "nothing — this is the daily self-test",
        # The deployed value, not a stand-in: the render leg checks that the page copy
        # still carries this line, and a stand-in would make that check vacuous.
        "owner": _escalation_owner(),
        "pending_send": True,
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
    try:
        from notify.explain import humanise
    except Exception as e:
        return leg("RENDER  plain-English layer imports", False, f"{type(e).__name__}: {e}")
    try:
        deny, allow = _pii_rules()
    except Exception as e:
        return leg("RENDER  PII rules loaded", False, f"{type(e).__name__}: {e}")
    owner = _escalation_owner()
    ok = True

    # Both tiers that reach a person, not just `notify`. explain.py branches on tier for
    # the icon and the urgency line, and `page` is the branch nobody sees until 03:00.
    for tier in ("notify", "page"):
        f = synthetic_finding(tier)
        try:
            human = humanise(dict(f))
        except Exception as e:
            ok &= leg(f"RENDER  humanise() runs on a {tier} finding", False,
                      f"{type(e).__name__}: {e}")
            continue
        rendered = telegram.render(dict(f))
        ok &= leg(f"RENDER  humanise() produces a {tier} message", bool(human and human.strip()),
                  f"{len(human or '')} chars")
        # render() swallows a humanise() exception and quietly returns the terse form.
        # Equal output is the only proof the plain-English layer actually ran.
        ok &= leg(f"RENDER  no silent fallback to the terse renderer ({tier})", rendered == human)
        ok &= leg(f"RENDER  the {tier} message fits one Telegram message", len(rendered) <= 4096,
                  f"{len(rendered)} chars")
        hits = [(n, m.group(0)[:40]) for ln in rendered.splitlines() if not allow.search(ln)
                for n, rx in deny for m in [rx.search(ln)] if m]
        hits += [("wallet address", a) for a in re.findall(r"0x[0-9a-f]{40}", rendered, re.I)]
        ok &= leg(f"RENDER  the {tier} message carries no PII", not hits, str(hits[:3]))
        if tier == "page":
            ok &= leg("RENDER  a page arrives as a page, not as a notify",
                      "\U0001f6a8" in rendered and "Needs attention now" in rendered)
            # phase3 §1.2: every page carries an escalation line. On 20 Aug 2.4 M of the
            # 2.66 M MOCA left AFTER the first page fired — what was missing was
            # somebody with the authority to stop it, not a detection — so a page
            # that quietly stopped carrying the line would be the August failure again,
            # invisibly. "UNASSIGNED" counts: a page admitting nobody is named is honest,
            # and it is what run.py stamps when the file names nobody. What must never
            # happen is the line vanishing.
            ok &= leg("RENDER  the page copy still carries an escalation contact",
                      owner in rendered or "UNASSIGNED" in rendered,
                      rendered[-90:].replace("\n", " "))
    return ok


# ------------------------------------------------------------------ leg 2

@contextlib.contextmanager
def _isolated(tmp, banner, asked):
    """The REAL send path, pointed at a temporary state file and a harmless message.

    telegram.render is swapped for a self-test banner for the duration: the state
    machine (write-before-send, send_ok, tg_message_id, by_message) is what is under
    test, and a message shaped like a real alert must never reach the channel — a
    screenshot of one forwards as a live page, and deleteMessage recalls nothing.

    `asked` collects the `silent` each caller REQUESTED, before it is overridden below.
    That list is the only honest evidence a live run can produce about sound, and it is
    what the page leg checks."""
    from notify import telegram
    o_state, o_render, o_send = telegram.STATE, telegram.render, telegram.send
    o_log = telegram._log_out
    try:
        telegram.STATE = tmp
        telegram.render = lambda _f: banner

        # The workflow header says this job "posts nothing to the channel on success".
        # A `notify`- or `page`-tier finding outside an incident sends LOUD, so every
        # phone in the group pinged daily — and the banner explaining it was deleted a
        # second later, leaving a notification for a message that no longer exists. The
        # real send path is still exercised; only the notification is suppressed.
        def _silenced(text, photo=None, silent=False):
            asked.append(bool(silent))
            return o_send(text, photo=photo, silent=True)
        telegram.send = _silenced

        # The ledger must record this as a SELF-TEST, not as an alert about the
        # synthetic case: the case is swept a second later, so a person replying to
        # the banner would otherwise be matched to an id that no longer exists.
        # Recorded, not skipped — an unrecorded message is an unanswerable one.
        telegram._log_out = lambda r, kind, case_id=None, reply_to=None: o_log(r, "test", None, reply_to)
        yield telegram
    finally:
        telegram.STATE, telegram.render, telegram.send = o_state, o_render, o_send
        telegram._log_out = o_log


def _isolation_ok(tmp):
    """True only when the send about to happen cannot reach the live state file.

    send_pending() writes `incident`, `by_message` and every `pending_send` flag into
    whatever telegram.STATE points at. If the redirection in _isolated() ever regresses,
    one self-test run opens an incident in the LIVE state, and for the next six hours
    every real alert goes out SILENTLY under a header nobody received. That is the
    loudest damage this file could do, so it is checked before the send rather than
    discovered afterwards from the wreckage."""
    from notify import state_sync, telegram
    return telegram.STATE == tmp and pathlib.Path(tmp).resolve() != state_sync.STATE.resolve()


def _live_state_bytes():
    """The REAL alerts/state.json as bytes, or None when there is no file.

    Read once before the sending legs and once after, so the run can SHOW it wrote
    nothing there rather than assert it. What it is really watching for is a stray
    `incident` object — see _isolation_ok."""
    from notify import state_sync
    try:
        return state_sync.STATE.read_bytes()
    except OSError:
        return None


def leg_deliver(tmpdir):
    """The `notify` tier: one message, no chart, through the real API."""
    tmp = tmpdir / "state.json"
    f = synthetic_finding()
    tmp.write_text(json.dumps({"open": {SYNTH_KEY: f}, "sent": {},
                               "telegram_offset": 0, "version": 1}, indent=1))
    with _isolated(tmp, BANNER, []) as tg:
        if not _isolation_ok(tmp):
            return leg("DELIVER the live state file is not the one being written", False,
                       f"telegram.STATE={tg.STATE}"), None
        rc = tg.send_pending()

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

def _synthetic_chart(path):
    """Draw the page's chart with the REAL renderer (detect/views.py).

    Nothing else exercises it: tests/test_notify.py only greps its source for the
    threshold line and the organic band. A renderer that stopped working — a
    matplotlib pin that moved, an Agg backend missing from the runner — costs every
    page its chart and says nothing, because run.py records the failure as
    `view_png = None` and send() then quietly posts the text alone."""
    try:
        sys.path.insert(0, str(ROOT / "detect"))
        import views
        from signals import Finding
    except Exception as e:
        print(f"selftest: the chart renderer did not import ({type(e).__name__}: {e})",
              file=sys.stderr)
        return False
    f = synthetic_finding("page")
    return bool(views.render(Finding(signal="selftest", key=f["key"], tier="page",
                                     value=f["value"], ts=f["ts"]), None, str(path)))


def leg_page(tmpdir):
    """The `page` tier: the twin photo+text path, live.

    Page is the tier with sound on and the only one that posts TWO messages for one
    finding — the text, then the chart as a reply to it, because a money-bearing
    alert exceeds Telegram's 1024-character caption limit. Neither half is touched by
    leg 2 (a short message with no photo takes a different branch of send()), and
    tests/test_notify.py stubs telegram.send outright, so the multipart upload, the
    reply threading and the second message id have never been proven against the real
    API by anything at all.

    Sound is the one property that cannot be proven live: proving it means pinging every
    phone in the group at 01:23 every night, and a channel people mute is worse than no
    channel. So the `silent` the send path ASKED for is recorded and checked, and the ask
    itself is refused."""
    f = synthetic_finding("page")
    png = tmpdir / "page.png"
    # Absolute, unlike run.py's repo-relative path: send() resolves a photo path against
    # the working directory, and this leg must not depend on where it was started.
    f["view_png"] = str(png)
    ok = leg("PAGE    the chart renderer produced a PNG", _synthetic_chart(png) and png.exists(),
             f"{png.stat().st_size if png.exists() else 0} bytes")
    if not ok:
        return ok, []

    tmp = tmpdir / "state-page.json"
    tmp.write_text(json.dumps({"open": {SYNTH_PAGE_KEY: f}, "sent": {},
                               "telegram_offset": 0, "version": 1}, indent=1))
    asked = []
    with _isolated(tmp, PAGE_BANNER, asked) as tg:
        if not _isolation_ok(tmp):
            return leg("PAGE    the live state file is not the one being written", False,
                       f"telegram.STATE={tg.STATE}"), []
        rc = tg.send_pending()

    s = json.loads(tmp.read_text())
    sent = (s.get("open") or {}).get(SYNTH_PAGE_KEY) or {}
    mid = sent.get("tg_message_id")
    mapped = sorted(int(m) for m, fid in (s.get("by_message") or {}).items()
                    if fid == SYNTH_PAGE_ID)
    ok &= leg("PAGE    send_pending() exits clean", rc == 0, f"rc={rc}")
    ok &= leg("PAGE    Telegram accepted the page (ok:true from the API)",
              sent.get("send_ok") is True, sent.get("send_error") or "")
    ok &= leg("PAGE    the page tier asked for sound (this run refused it)",
              asked == [False], f"silent requested: {asked}")
    # Two ids, or the chart never left. The chart is the most reply-attractive object on
    # the page, so an unmapped one turns a reader's "contained" into "I cannot tell which
    # case that was".
    ok &= leg("PAGE    the chart was delivered as its own message under the text",
              len(mapped) == 2, f"ids mapped to the case: {mapped}")
    ok &= leg("PAGE    the text message is the one the case maps back to",
              bool(mid) and mid in mapped, f"tg_message_id={mid}")
    ok &= leg("PAGE    the page was cleared from pending", not sent.get("pending_send"))
    return ok, (mapped or ([mid] if mid else []))


# ------------------------------------------------------------------ leg 4

def leg_dispatch():
    from notify.request_enrichment import dispatch
    from notify.state_sync import _pat
    pat = _pat()
    if not pat:
        return leg("DISPATCH tier-2 enrichment accepted", False,
                   "PRIVATE_REPO_PAT is not set — the tier-2 leg cannot be tested")
    ok, detail = dispatch(SYNTH_ID, pat)
    return leg("DISPATCH tier-2 enrichment accepted by the private repo", ok, detail)


# ------------------------------------------------------------------ leg 5

def delete_messages(mids):
    """Remove this run's own messages from the channel, highest id first.

    Highest first because the page leg's chart is sent as a REPLY to its text message:
    delete the parent first and the chart is left quoting a message that is no longer
    there. One explanation is posted for the whole run rather than one per stuck id —
    the channel does not need three messages to be told the same thing once."""
    ids = sorted({m for m in (mids or []) if m}, reverse=True)
    if not ids:
        return True
    from notify import telegram
    stuck = []
    for mid in ids:
        r = telegram._post("deleteMessage", {"chat_id": telegram.CHAT(), "message_id": mid})
        if not r.get("ok"):
            stuck.append(f"{mid} ({str(r.get('error'))[:40]})")
    if not stuck:
        return True
    # Do not leave a message the channel cannot explain — and do not let the explanation
    # itself fail silently. The stuck ids carry a raw Telegram error string, which goes
    # out under parse_mode=HTML: unescaped, an error containing < produces a 400 that
    # send() swallows, and the leftover page-shaped banner is left standing with nothing
    # explaining it. That is fix-round critic #6 exactly, in the one message whose only
    # job is to explain a page-shaped banner (rule 2).
    try:
        from explain import _esc
    except ImportError:
        from notify.explain import _esc
    note = telegram.send("\U0001f9ea The message(s) above were the daily self-test, not an alert. "
                         "I could not delete " + _esc("; ".join(stuck)) + ".", silent=True)
    telegram._log_out(note, "test")
    if not note.get("ok"):
        print(f"selftest: the deletion notice ITSELF failed ({note.get('error')}) — "
              f"a page-shaped banner is standing in the channel with nothing explaining it",
              file=sys.stderr)
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
                     "PAGE": "sending the loudest kind of alert, with its chart",
                     "DISPATCH": "asking for the follow-up account detail",
                     "CLEAN": "cleaning up after itself",
                     "RUN": "running at all"}
            parts = sorted({plain.get(n.split()[0], n.split()[0]) for n in failed})
            telegram._log_out(telegram.send(
                "\U0001f534 <b>My daily check on myself failed.</b>\n"
                f"What did not work: <b>{'; '.join(parts) or 'unknown'}</b>\n\n"
                "This is the check that proves alerts can still reach this channel. Until it "
                "passes, assume I may be watching the chain without being able to tell anyone "
                "\u2014 so treat quiet as unknown, not as safe.\n"
                f"Please tell <b>{_owner()}</b>."
                + (f"\nRun log: {os.environ['RUN_URL']}" if os.environ.get("RUN_URL") else ""),
                silent=False), "notice")
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
    mids = []
    live_before = _live_state_bytes()
    ok = True
    try:
        ok &= leg_render()
        delivered, mid = leg_deliver(tmpdir)
        ok &= delivered
        if mid:
            mids.append(mid)
        paged, page_mids = leg_page(tmpdir)
        ok &= paged
        mids += page_mids
        ok &= leg_dispatch()
    except Exception as e:
        ok = leg("RUN     self-test completed without crashing", False, f"{type(e).__name__}: {e}")
    finally:
        # Cleanup is not conditional on success: a half-failed run is exactly when
        # synthetic state gets left behind.
        cleaned = delete_messages(mids)
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Read BEFORE the sweep, which legitimately pulls and rewrites the live state:
        # afterwards there is nothing left to compare it against.
        untouched = _live_state_bytes() == live_before
        swept = sweep()
        ok &= leg("CLEAN   synthetic message(s) removed from the channel", cleaned)
        ok &= leg("CLEAN   the live state file was not written by this run", untouched)
        ok &= leg("CLEAN   no synthetic finding left in the state", swept)
    report(ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
