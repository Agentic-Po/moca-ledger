#!/usr/bin/env python3
"""Behaviour tests for the alerting layer — the parts that decide whether the   # pii-ok
channel goes quiet, lies, or leaks.

Every check here corresponds to a defect that was found by review rather than by a
run, which is exactly the class of defect that stays invisible until an incident.
Nothing here touches the real alerts/state.json and nothing sends to Telegram: the
sender is stubbed and every state file is a temporary one.

Usage:  python3 tests/test_notify.py        (exit 1 on any failure)
"""
import contextlib, json, os, pathlib, re, sys, tempfile, time, urllib.error, io, datetime as dt

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notify import telegram, commands, state_sync, msglog, watchdog   # noqa: E402

# The message ledger is redirected for the whole test process, before any test runs.
# A synthetic message_id written into the real ledger would collide with a genuine
# Telegram id and resolve a real person's reply to a fake case.
_LEDGER = pathlib.Path(tempfile.mkdtemp(prefix="moca-msglog-"))
msglog.LOCAL, msglog.INDEX, msglog._INDEX = _LEDGER, _LEDGER / "index.json", None

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


# ---------------------------------------------------------------- harness

SENT = []


def _finding(i, tier="page", signal=None, **kw):
    f = {"id": f"t{i:04d}", "key": "0x%040x" % (0xa000 + i), "signal": signal or "10",
         "tier": tier, "value": 1.0 + i, "window": "6h", "ts": 1755900000 + i,
         "first_ts": f"2026-08-21T{i % 24:02d}:00:00+00:00", "type_verified": False,
         "detail": "synthetic", "headline": ["synthetic"], "pending_send": True}
    f.update(kw)
    return f


class Bed:
    """A temporary state file plus a stubbed sender. `fail` fails the Nth send."""

    def __init__(self, findings=(), fail_index=None, **state):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="moca-notify-"))
        self.path = self.dir / "state.json"
        s = {"open": {f["key"]: f for f in findings}, "telegram_offset": 0, "version": 1}
        s.update(state)
        self.path.write_text(json.dumps(s, indent=1))
        self.fail_index = fail_index

    def __enter__(self):
        self._state, self._send = telegram.STATE, telegram.send
        telegram.STATE = self.path
        SENT.clear()

        def fake_send(text, photo=None, silent=False):
            # `photo` is recorded, not dropped: which tier gets a chart is a
            # behaviour now, and a stub that forgets the argument cannot test it.
            SENT.append({"text": text, "silent": silent, "photo": photo})
            if self.fail_index is not None and len(SENT) - 1 == self.fail_index:
                return {"ok": False, "error": "http 400"}
            return {"ok": True, "result": {"message_id": 1000 + len(SENT)}}

        telegram.send = fake_send
        return self

    def __exit__(self, *a):
        telegram.STATE, telegram.send = self._state, self._send

    def state(self):
        return json.loads(self.path.read_text())


def run(bed):
    with bed:
        telegram.send_pending()
        return bed.state(), list(SENT)


# ---------------------------------------------------------------- incident mode

def t_incident():
    print("\nincident mode")
    now = time.time()

    # A cash-out is tier `notify`; a burst means >= 6 pages are pending. It must still
    # get a slot and must still sound.
    cash = _finding(99, tier="notify", signal="S-X", value=250000,
                    headline=["MOCA +250,000.0000 (balance moved)", "role: exchange_deposit"])
    pages = [_finding(i) for i in range(8)]
    s, sent = run(Bed(pages + [cash]))
    body = "\n".join(x["text"] for x in sent)
    got = (s["open"][cash["key"]].get("last_sent") is not None)
    check("a cash-out is sent during a burst of pages", got,
          f"pending_send={s['open'][cash['key']].get('pending_send')}")
    loudness = [x["silent"] for x in sent if "cash-out" in x["text"] or "exchange" in x["text"]]
    check("the cash-out alert sounds", any(v is False for v in loudness) or
          any(("Sounding despite incident mode" in x["text"]) and not x["silent"] for x in sent))

    # A dust deposit must not burn the siren.
    dust = _finding(98, tier="notify", signal="S-X", value=0.000001,
                    headline=["MOCA +0.000001 (balance moved)", "role: exchange_deposit"])
    check("dust below cashout_sound_min is not 'the first cash-out'",
          telegram._cashout_addr(dust) is None)
    check("a material arrival at an exchange IS a cash-out",
          telegram._cashout_addr(cash) is not None)
    check("a balance DROP at an exchange address is not a cash-out arriving",
          telegram._cashout_addr(dict(cash, value=-250000)) is None)

    # Burning it at one destination must not disarm it at another.
    inc = {"cashouts": [telegram._cashout_addr(cash)], "classes": ["S-X"]}
    other = dict(cash, key="0x%040x" % 0xbeef)
    check("the cash-out siren is per destination, not one global latch",
          telegram._sound_reason(other, inc) is not None,
          str(telegram._sound_reason(other, inc)))

    # An incident survives a quiet run and ends on the TTL.
    inc_on = {"started": "2026-08-23T00:00:00", "runs": 3, "classes": ["10"],
              "cashouts": [], "last_ts": now - 60}
    _, on, _ = telegram._incident_state({"incident": dict(inc_on)}, [], 0, now)
    check("one quiet run does NOT end an incident", on)
    _, on2, _ = telegram._incident_state({"incident": dict(inc_on, last_ts=now - 7 * 3600)}, [], 0, now)
    check("six quiet hours DOES end an incident (the TTL is reachable)", not on2)

    # ---- the run that OPENS an incident sends ONE loud message, not N.
    # Po's decision 1. This test previously asserted the opposite and CI defended
    # it: with classes seeded empty, every distinct signal already pending counted
    # as "first of its class" and the opening run rang once per signal.
    s, sent = run(Bed([_finding(i, signal=str(10 + i)) for i in range(5)]))
    header = sent[0]["text"] if sent else ""
    check("the header is the first message of an incident run", "Incident mode" in header)
    check("the opening run sends exactly one loud message — the header",
          sum(1 for x in sent if not x["silent"]) == 1,
          f"{sum(1 for x in sent if not x['silent'])} loud of {len(sent)}")
    check("the opening run seeds the classes it is showing",
          sorted((s.get("incident") or {}).get("classes") or []) == sorted(str(10 + i) for i in range(5)),
          str((s.get("incident") or {}).get("classes")))

    # A class that turns up in a LATER run is news and still sounds.
    later = _finding(70, signal="S-NEW")
    s5, sent5 = run(Bed([later], loud_held=0, arrivals_prev=1,
                        incident=dict(inc_on, last_ts=now)))
    check("a signal class first seen in a later run does sound",
          any(not x["silent"] and "first S-NEW alert" in x["text"] for x in sent5),
          "; ".join(f"{x['silent']}" for x in sent5))

    # The invented fifth trigger is gone.
    check("'largest payout total' is not a sound reason any more",
          telegram._sound_reason(_finding(71, signal="10", moca_since=9e9),
                                 {"classes": ["10"], "cashouts": []}) is None)

    # ---- Po's fourth trigger: the rate doubling. Computed AND sounded, once.
    quad = [_finding(80 + i, signal="10") for i in range(4)]
    s6, sent6 = run(Bed(quad, loud_held=0, arrivals_prev=2,
                        incident=dict(inc_on, last_ts=now)))
    dbl = [x for x in sent6[1:] if telegram.DOUBLED_SOUND in x["text"]]
    check("a doubled arrival rate makes exactly one finding sound", len(dbl) == 1,
          f"{len(dbl)} findings cite the doubling")
    check("the doubling sound is actually audible", dbl and dbl[0]["silent"] is False)
    check("the header advertises the doubling as policy, not an invented rule",
          "the rate of new alerts doubling" in sent6[0]["text"]
          and "bigger than any so far" not in sent6[0]["text"])
    s7, sent7 = run(Bed([_finding(90 + i, signal="10") for i in range(4)],
                        loud_held=0, arrivals_prev=99, incident=dict(inc_on, last_ts=now)))
    check("a flat run does not cite the doubling",
          not any(telegram.DOUBLED_SOUND in x["text"] for x in sent7))

    # The header's promise and the sends must be the same decision.
    for label, msgs in (("opening", sent), ("doubled", sent6), ("flat", sent7)):
        head = msgs[0]["text"]
        m = re.search(r"(\d+) below will sound", head)
        promised = int(m.group(1)) if m else 0
        actual = sum(1 for x in msgs[1:] if not x["silent"])
        check(f"the {label} header's sound count matches what sounds", promised == actual,
              f"header promised {promised}, {actual} sounded")

    # Counts describe arrivals, not the backlog.
    check("the header counts arrivals, not queue depth", "5 new alert(s) this run" in header,
          header.splitlines()[0])
    s2, sent2 = run(Bed([_finding(i, signal=str(10 + i)) for i in range(5)],
                        loud_held=4, arrivals_prev=1, incident=dict(inc_on)))
    check("carried-over findings are not counted as new again",
          "1 new alert(s) this run" in sent2[0]["text"], sent2[0]["text"].splitlines()[0])

    # A header that did not deliver must leave the findings loud.
    s3, sent3 = run(Bed([_finding(i) for i in range(5)], fail_index=0))
    check("an undelivered header leaves every finding LOUD",
          all(x["silent"] is False for x in sent3[1:]),
          f"{sum(1 for x in sent3[1:] if x['silent'])} silent")

    # Money scope is stated, not implied.
    s4, sent4 = run(Bed([_finding(i, moca_since=1000.0 * (i + 1), rate_per_h=10.0) for i in range(5)]))
    h = sent4[0]["text"]
    check("the money figure states that it is Treasury payouts only",
          "Treasury payouts to these wallets only" in h)
    check("the money figure states that its windows differ per wallet",
          "not\none clean window" in h or "not one clean window" in h.replace("\n", " "))


def t_charts():
    print("\ncharts ride the page tier only")
    png = "incidents/2026-08-21/1320-10-a0000028/view.png"

    # Two loud findings is below incident_mode_min, so there is no header and every
    # message below is one alert. The wallet key is rendered into the alert text, so
    # each message can be matched back to its finding without relying on send order.
    page = _finding(40, tier="page", view_png=png)
    quiet = _finding(41, tier="notify", signal="S-A", view_png=png)
    s, sent = run(Bed([page, quiet]))
    got = {}
    for x in sent:
        for f in (page, quiet):
            if f["key"] in x["text"]:
                got[f["id"]] = x.get("photo")
    check("both alerts were sent and matched back to their finding",
          sorted(got) == sorted([page["id"], quiet["id"]]),
          f"matched {sorted(got)} of {len(sent)} message(s)")
    check("a page-tier alert still carries its chart", got.get(page["id"]) == png,
          f"photo={got.get(page['id'])}")
    check("a notify-tier alert is sent with no chart", got.get(quiet["id"]) is None,
          f"photo={got.get(quiet['id'])}")

    # The cut is from the CHANNEL, not from the evidence record: incidents/ still
    # holds the PNG and state still holds the path that points at it.
    check("the notify finding keeps its view_png (incidents/ stays complete)",
          s["open"][quiet["key"]].get("view_png") == png,
          str(s["open"][quiet["key"]].get("view_png")))

    # An escalation rewrites `tier` on the stored finding and nothing else, so
    # gating on anything but the stored tier would send it without its chart.
    check("a case escalated notify -> page gets its chart back",
          telegram._chart_for({"tier": "page", "view_png": png,
                               "escalation": "activity after containment"}) == png)
    check("no chart means no detached chart message left unmapped to a case",
          telegram._chart_for({"tier": "notify", "view_png": png}) is None)
    check("a digest finding never carries a chart",
          telegram._chart_for({"tier": "digest", "view_png": png}) is None)


def t_dead_copy():
    print("\nsignal 14 is a field, not an alert")
    from notify import explain
    # detect/signals/outflow.py computes #14 on every run and returns no findings, so
    # alert copy for it could only be reached by a signal id nothing produces.
    # tests/test_gate.py holds the other half of this: that it stays that way.
    check("explain.py carries no alert copy for #14", "14" not in explain.SIGNALS,
          str(explain.SIGNALS.get("14"))[:60])
    check("explain.py carries no digest line for #14", "14" not in explain.DIGEST_LINE,
          str(explain.DIGEST_LINE.get("14"))[:60])

    # Deleting copy is only safe because the fallback is honest rather than silent.
    # A finding with an unrecognised signal must still render, must still show its
    # measurement, and must not be given a reassuring sentence nobody wrote for it.
    body = explain.humanise({"signal": "14", "tier": "notify", "value": 3.0,
                             "key": "platform", "detail": "x3 vs the 28-d median",
                             "type_verified": False})
    check("an unknown signal still renders, and says the measurement is all there is",
          explain.FALLBACK["title"] in body and "x3 vs the 28-d median" in body,
          body.splitlines()[0] if body else "")
    d = explain.digest([{"signal": "14", "tier": "digest", "detail": "x3"}])
    check("an unknown signal in the digest is counted, never dropped in silence",
          explain.FALLBACK["title"] in d and "1 event(s)" in d, d[:100])


def t_holding():
    print("\nnothing is dropped in silence")
    soon = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=6)).isoformat()
    f = _finding(1, tier="page", snooze_until=soon)
    s, sent = run(Bed([f]))
    check("a snoozed finding stays PENDING (it sends when the snooze expires)",
          s["open"][f["key"]].get("pending_send") is True,
          f"suppressed={s['open'][f['key']].get('suppressed')}")
    acked = _finding(2, tier="page", ack_by="123")
    s, _ = run(Bed([acked]))
    check("an acked finding is cleared (a person decided that)",
          s["open"][acked["key"]].get("pending_send") is False)

    s, sent = run(Bed([_finding(3, tier="page")],
                      retired_notice={"total": 1102, "unacked": 1102}))
    check("findings aged out by the size cap are ANNOUNCED, not only logged",
          any("aged out of my memory" in x["text"] for x in sent))
    check("the retirement notice is cleared once sent", not (s.get("retired_notice")))


def t_alarm():
    print("\nthe send-failure alarm")
    s, sent = run(Bed([_finding(i) for i in range(2)], fail_index=0))
    check("a failed send raises the alarm", any("failed to send" in x["text"] for x in sent))
    check("the alarm flag is set", s.get("send_failure_alarmed") is True)
    s2, sent2 = run(Bed([_finding(9)], send_failure_alarmed=True,
                        **{"open_extra": None} if False else {}))
    check("a clean run clears the alarm so the NEXT failure is announced too",
          s2.get("send_failure_alarmed") is False)


def t_post():
    print("\nthe sender")
    real = telegram.urllib.request.urlopen

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", {},
                                     io.BytesIO(b"<html>429 nginx</html>"))
    telegram.urllib.request.urlopen = boom
    slept = telegram.time.sleep
    telegram.time.sleep = lambda *_a, **_k: None      # do not spend the backoff in CI
    try:
        r = telegram._post("sendMessage", {"chat_id": "1", "text": "x"})
        ok = isinstance(r, dict) and r.get("ok") is False
    except Exception as e:
        ok = False
        r = f"{type(e).__name__}"
    finally:
        telegram.urllib.request.urlopen = real
        telegram.time.sleep = slept
    check("a 429 with a non-JSON body returns an error instead of raising", ok, str(r)[:60])


# ---------------------------------------------------------------- the reply surface

def t_replies():
    print("\nthe reply surface")
    for text, want in [("contained", "contained"),
                       ("contained - restricted the wallet", "contained"),
                       ("I closed the support ticket", None),
                       ("closed the ticket, still watching", None),
                       ("not yet contained", None),
                       ("nothing is resolved", None),
                       ("it isn't resolved yet", None),
                       ("we haven't contained it", None),
                       ("reported, not contained", None),
                       ("this looks like an attack, pausing now", None)]:
        got, _ = commands.read_status(text)
        check(f"reply {text!r} -> {want}", got == want, f"got {got}")

    s = {"open": {"0x" + "a" * 40: {"id": "c1", "key": "0x" + "a" * 40},
                  "0x" + "b" * 40: {"id": "c2", "key": "0x" + "b" * 40}}}
    check("/close 0x cannot close an arbitrary case", commands.find(s, "0x")[1] is None)
    check("an unambiguous long prefix still works", commands.find(s, "0xaaaaaaaa")[1] is not None)
    check("an exact id still works", commands.find(s, "c2")[1] is not None)

    long = "\n".join(f"<b>line {i}</b> xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" for i in range(400))
    chunks = commands._chunks(long)
    check("a long reply is split on line boundaries, never mid-tag",
          len(chunks) > 1 and all(c.count("<b>") == c.count("</b>") for c in chunks),
          f"{len(chunks)} chunks")
    check("a human's note is escaped before it re-enters HTML",
          commands._esc('contained <see ticket>') == "contained &lt;see ticket&gt;")

    big = {"open": {f"0x{i:040x}": {"id": f"c{i}", "key": f"0x{i:040x}", "tier": "page",
                                    "signal": "10", "detail": "top1=62% n=113"}
                    for i in range(400)}}
    txt = commands.cases_text(big)
    check("/cases is bounded rather than truncated", len(txt) < 8000, f"{len(txt)} chars")
    check("/cases says how many it did not show", "more, oldest shown first" in txt)
    check("/cases does not print the raw detector detail", "top1=" not in txt)
    st = commands.status_text(big)
    check("/status does not print block lag or row counts",
          "lag" not in st and "rows" not in st and "mindset" not in st)


def t_reply_time():
    """A decision is stamped from when the person typed it, not when we read it."""
    print("\nthe baseline a human decision is judged against")
    typed = int(dt.datetime(2026, 8, 23, 2, 20, tzinfo=dt.UTC).timestamp())
    alert = typed - 900            # the alert they were replying to: value 100
    later = typed + 1800           # a re-fire that landed while the reply sat unread

    f = {"id": "c1", "key": "0x" + "a" * 40, "value": 900.0,
         "sends": [[later, 900.0], [alert, 100.0]]}
    s = {"open": {f["key"]: f}}
    commands.set_status(s, "c1", "contained", "u1", "wallet restricted", when=typed)
    check("status_ts is the moment the reply was sent, not the moment it was read",
          f["status_ts"].startswith("2026-08-23T02:20"), f["status_ts"])
    check("the baseline is the value the reader was actually looking at",
          f["value_at_status"] == 100.0, str(f["value_at_status"]))
    check("a re-fire during the polling gap is not folded into the baseline",
          f["value_at_status"] != 900.0)

    # explain.py must now describe the growth the reader did NOT see.
    from notify import explain
    words = explain._grew_words(f)
    check("the reader is told it grew, not that it is about the same level",
          "9.0×" in words or "9.0\u00d7" in words, words)

    # No send history (a finding from before this existed) still records something.
    g = {"id": "c2", "key": "0x" + "b" * 40, "value": 42.0}
    commands.set_status({"open": {g["key"]: g}}, "c2", "reported", "u1", "", when=None)
    check("a finding with no send history falls back to the live value",
          g["value_at_status"] == 42.0)
    check("a missing message.date falls back to now, it does not crash",
          g["status_ts"][:4] == "20" + str(dt.datetime.now(dt.UTC).year)[2:])


def t_reply_delivery():
    """An undelivered reply must not be a green run."""
    print("\nan answer that never arrived")
    real_api, real_chat = commands.api, commands.CHAT
    try:
        commands.api = lambda method, **p: {"ok": False, "error": "http 400 can't parse entities"}
        commands.CHAT = lambda: "-100"
        commands.UNDELIVERED.clear()
        # The failure is the point of the test, so its stderr line is swallowed here.
        # Left through, it prints "the person who asked got nothing" into the log of a
        # GREEN run, which is exactly the sentence an operator must be able to trust.
        with contextlib.redirect_stderr(io.StringIO()):
            failed = commands.reply("hello", 1)
        check("reply() reports failure", failed is False)
        check("a failed reply is recorded for main() to act on", len(commands.UNDELIVERED) == 1)
        commands.UNDELIVERED.clear()

        # `/close a<b>c` names no case, so the bot echoes what was typed. Unescaped,
        # that is a 400 "can't parse entities" that api() swallows, and the channel
        # goes silent on a green run.
        seen = []
        commands.api = lambda method, **p: (seen.append(p.get("text", "")) or {"ok": True, "result": {}})
        os.environ["TELEGRAM_ACK_USER_IDS"] = "999"
        commands.handle({"open": {}, "telegram_offset": 0},
                        {"text": "/close a<b>c", "message_id": 5, "chat": {"id": "-100"},
                         "from": {"id": "999"}})
        echoed = "\n".join(seen)
        check("the bot echoed the unmatched id back", "a&lt;b&gt;c" in echoed, echoed[:90])
        check("raw user input never re-enters HTML as markup", "a<b>c" not in echoed)
    finally:
        commands.api, commands.CHAT = real_api, real_chat
        os.environ.pop("TELEGRAM_ACK_USER_IDS", None)
        commands.UNDELIVERED.clear()


# ---------------------------------------------------------------- leaks

def t_leaks():
    print("\nwhat a forwarded screenshot hands over")
    from notify import explain
    ev = {"signal": "EV", "value": 340, "detail": "340/24h >= 3 x", "key": "platform",
          "tier": "page", "window": "24h"}
    m = explain.measured(ev)
    check("the EV measurement does not print the threshold multiple",
          "three times" not in m and "3 x" not in m and ">=" not in m, m)
    comp = {"signal": "composite", "value": 2, "detail": "10, S-A", "key": "e9c23aac06",
            "tier": "page", "window": "6h"}
    m2 = explain.measured(comp)
    check("the composite measurement does not print the signal codes",
          "S-A" not in m2 and "10, " not in m2, m2)
    src = (ROOT / "detect" / "views.py").read_text()
    check("no chart draws a labelled threshold",
          "axhline" not in src and "page threshold (" not in src)
    check("no chart draws the organic p50-p95 band", "axhspan" not in src)


# ---------------------------------------------------------------- the money line

def t_price():
    """A dollar figure must say how old the price behind it is."""
    print("\nhow old the money is")
    sys.path.insert(0, str(ROOT / "detect"))
    import price

    def note_at(hours):
        real = price.age_h
        try:
            price.age_h = lambda doc=None: hours
            return price.price_of("MOCA")[1]
        finally:
            price.age_h = real

    fresh, day_old, stale = note_at(2), note_at(20), note_at(70)
    check("a fresh price says how old it is", "2 h old" in fresh, fresh)
    check("a price inside the day is not called stale", "stale" not in day_old, day_old)
    check("a price older than a day is marked stale", "stale" in stale, stale)
    check("a stale price states its age, not just its date", "3 d old" in stale, stale)
    check("the stale marker is reachable well under three days",
          price.STALE_DAYS <= 1, f"STALE_DAYS={price.STALE_DAYS}")

    # The note is what both renderers append to the money line, verbatim.
    from notify import explain
    lines = explain.money_lines({"moca_since": 38141.0, "first_ts": "2026-08-21T00:00:00+00:00",
                                 "type_verified": False})
    check("the money line carries the price note",
          any("price" in l for l in lines), " | ".join(lines)[:120])


def t_dead_copy():
    """No alert card may exist for a signal that can never fire."""
    print("\ncopy for alerts that cannot happen")
    from notify import explain
    emitted = set()
    pat = re.compile(r"""Finding\(\s*["']([^"']+)""")
    for mod in (ROOT / "detect" / "signals").glob("*.py"):
        emitted |= set(pat.findall(mod.read_text()))
    # Signals built from a variable name are resolved by hand; keep the list honest
    # rather than silently passing because a regex missed one.
    # Signals whose id is built from a variable rather than a literal — resolved by
    # hand so a regex miss cannot silently pass the whole check.
    dynamic = {"10i", "10n", "S-Q2"}
    for sid in sorted(explain.SIGNALS):
        if sid in dynamic or sid.startswith("S-") or not sid.isdigit():
            continue
        check(f"signal {sid} has a card because something can emit it",
              sid in emitted, "no Finding(...) constructs it" if sid not in emitted else "")
    # Both cut for the same reason: their own recommended action was "Nothing", and a
    # card for an alert that can never arrive is copy a reader can still be shown by
    # the terse fallback — which announces itself as a failure — for no event at all.
    for gone in ("14", "7"):
        check(f"#{gone}'s alert card is gone", gone not in explain.SIGNALS
              and gone not in explain.DIGEST_LINE)
    check("the safety boundary on composite is named as one",
          "SAFETY BOUNDARY" in (ROOT / "detect" / "signals" / "composite.py").read_text())


def t_local_send_guard():
    """A sender run by hand must not be able to page the live channel."""
    print("\nrunning a sender by hand")
    import os
    from notify import watchdog as wd
    keep = os.environ.pop("GITHUB_ACTIONS", None)
    argv = list(sys.argv)
    try:
        sys.argv = ["watchdog.py"]
        check("the watchdog refuses to post from a laptop", not wd._may_send())
        sys.argv = ["watchdog.py", "--force"]
        check("--force is still available for a deliberate local test", wd._may_send())
        sys.argv = ["watchdog.py"]
        os.environ["GITHUB_ACTIONS"] = "true"
        check("a scheduled run still posts normally", wd._may_send())
    finally:
        sys.argv = argv
        os.environ.pop("GITHUB_ACTIONS", None)
        if keep is not None:
            os.environ["GITHUB_ACTIONS"] = keep


def t_platform_signals_can_speak_twice():
    """A signal keyed on a literal string must not be one-shot for the life of the system."""
    print("\nthe platform-wide pagers")
    sys.path.insert(0, str(ROOT / "detect"))
    import run as RUN

    seeded = {"id": "4c8de53e7f", "key": "platform", "signal": "15", "tier": "page",
              "value": 1800.0, "ack_by": "go-live-seed", "ack_ts": "2026-08-22T11:21:16",
              "pending_send": False, "last_sent": None, "type_verified": False}
    check("a go-live seed does not permanently mute a signal",
          telegram._suppressed(dict(seeded), {}) is None,
          str(telegram._suppressed(dict(seeded), {})))
    check("a PERSON's ack is still permanent",
          telegram._suppressed(dict(seeded, ack_by="7788"), {}) == "acked")
    check("an ack_role is still permanent",
          telegram._suppressed(dict(seeded, ack_by=None, ack_role="oncall"), {}) == "acked")

    check("a platform-keyed finding is recognised as having a permanent id",
          RUN._stable_key({"key": "platform"}) and RUN._stable_key({"key": "platform:all"}))
    check("a wallet-keyed finding is not — a new wallet is a new case",
          not RUN._stable_key({"key": "0x" + "a" * 40}))

    # The re-arm itself: grew past the value on the message a person last saw.
    cur = dict(seeded, sends=[[1787000000, 1800.0]], value=2800.0)
    check("the baseline is the value on the last message that was actually sent",
          RUN._last_alerted_value(cur) == 1800.0)
    check("a platform signal that rises materially can alert again",
          RUN._grew(cur, RUN._last_alerted_value(cur), factor=RUN.RE_ARM_FACTOR))
    check("a platform signal that barely moves does not",
          not RUN._grew(dict(cur, value=1850.0), 1800.0, factor=RUN.RE_ARM_FACTOR))
    check("with no send history it falls back rather than crashing",
          RUN._last_alerted_value(dict(seeded)) is None)

    # Growth alone is not enough. #15 means "the payout worker hit its ceiling" — an
    # EVENT — and a second saturation at the same level is not 1.5x of the first. A
    # seeded record that was never delivered is therefore treated as absent entirely,
    # so the next fire arms itself the way any genuinely new finding would.
    st = {"open": {"k": dict(seeded)}}
    fresh = dict(seeded, value=1796.0)
    class _F:
        id = "4c8de53e7f"
        _state = fresh
        signal, key, tier, ts = "15", "platform", "page", 1787000000
    check("a seeded, never-sent, platform-keyed record is not treated as a live case",
          RUN._stable_key(seeded) and not seeded.get("last_sent")
          and seeded.get("ack_by") == RUN.SEED_ACK)
    check("a record a person actually received is still a live case",
          not (RUN._stable_key(dict(seeded, last_sent="2026-08-22T00:00:00"))
               and not dict(seeded, last_sent="x").get("last_sent")))

    # The live state today: every platform-keyed finding must be able to reach a person.
    import json as _json
    live = ROOT / "alerts" / "state.json"
    if live.exists():
        st2 = _json.loads(live.read_text())
        stuck = [f for f in (st2.get("open") or {}).values()
                 if str(f.get("key", "")).startswith("platform")
                 and telegram._suppressed(f, st2) == "acked"]
        check("no platform-wide signal is muted in the live state file",
              not stuck, f"{len(stuck)} muted: {sorted({str(f.get('signal')) for f in stuck})}")


def t_gate_failure():
    """A red behaviour gate must not be able to take the channel dark in silence."""
    print("\nwhen my own checks fail")
    wf = (ROOT / ".github" / "workflows" / "crawl.yml").read_text()

    def step(name):
        """The step's own keys — stopping at the next step OR the comment above it.
        PyYAML is not a dependency here (requirements.txt is matplotlib alone), and
        splitting on "- name:" alone swept in the comment that explains this very
        change, which contains the word it was looking for."""
        body = wf.split(f"- name: {name}\n", 1)[1].splitlines()
        out = []
        for line in body:
            s = line.strip()
            if s.startswith("#") or s.startswith("- name:"):
                break
            out.append(line)
        return "\n".join(out)

    check("the behaviour gate cannot stop the send path any more",
          "continue-on-error: true" in step("Alerting behaviour gate"))
    check("the PII gate still DOES stop it — a leak cannot be undone",
          "continue-on-error" not in step("PII gate"), step("PII gate").strip()[:60])
    check("a failed gate is announced in the channel, not only in the run log",
          "--gate-failed" in wf and "BEHAVIOUR_GATE" in wf)

    with Bed([]) as bed:
        telegram.gate_failed("https://example.invalid/run/1")
        first = list(SENT)
        check("the notice says alerts are still going out",
              first and "still being sent" in first[0]["text"], first[0]["text"][:70] if first else "")
        check("the notice sounds — it is not itself a quiet failure",
              first and first[0]["silent"] is False)
        check("it does not tell the reader silence is safe",
              first and "all-clear" in first[0]["text"] and "not" in first[0]["text"])
        SENT.clear()
        telegram.gate_failed("https://example.invalid/run/2")
        check("a gate stuck red does not repost every ten minutes", not SENT,
              f"{len(SENT)} repeat(s)")


# ---------------------------------------------------------------- the message ledger

def t_msglog():
    """Nothing lost, nothing unmatchable — and nothing unbounded."""
    print("\nthe message ledger")
    d = pathlib.Path(tempfile.mkdtemp(prefix="moca-msglog-t-"))
    o_local, o_index, o_ix, o_shard = msglog.LOCAL, msglog.INDEX, msglog._INDEX, msglog.SHARD_MAX
    try:
        msglog.LOCAL, msglog.INDEX, msglog._INDEX = d, d / "index.json", None

        msglog.record_out(100, "alert", case_id="c1")
        msglog.record_out(101, "chart", reply_to=100)          # the detached chart
        msglog.record_out(102, "digest")
        msglog.record_out(103, "tier2", reply_to=100)          # the private-side detail
        msglog.flush()
        check("a reply to the alert resolves", msglog.resolve(100) == "c1")
        check("a reply to its chart resolves through the parent",
              msglog.resolve(101) == "c1", str(msglog.resolve(101)))
        check("a reply to a tier-2 detail resolves through the parent",
              msglog.resolve(103) == "c1", str(msglog.resolve(103)))
        check("a reply to the digest resolves to no case", msglog.resolve(102) is None)
        check("but the digest can still be NAMED, so the bot need not say 'I cannot tell'",
              msglog.describe_words(102) == "the quiet daily digest",
              str(msglog.describe_words(102)))
        check("a message from before the ledger is honestly unknown",
              msglog.describe(9999) is None and msglog.resolve(9999) is None)

        # An unmatched reply is STORED, not discarded — that is the whole point.
        msglog.record_in({"update_id": 9, "message": {"message_id": 500, "text": "did it stop?",
                                                      "reply_to_message": {"message_id": 9999}}},
                         outcome="unmatched")
        check("an unmatched reply is kept so it can be linked afterwards",
              [r["text"] for r in msglog.unresolved()] == ["did it stop?"])
        msglog.link(9999, "c1"); msglog.flush()
        check("/link makes an unmatchable message resolve", msglog.resolve(9999) == "c1")

        # The size failure this file was rewritten to escape (fix-round critic #7).
        msglog.SHARD_MAX = 2000
        for i in range(400):
            msglog.record_out(1000 + i, "alert", case_id=f"c{i}")
        msglog.flush()
        shards = sorted(p.name for p in d.glob("out-*.jsonl"))
        check("the archive rolls to a new shard instead of growing one file",
              len(shards) > 1, f"{len(shards)} shards")
        check("no shard is anywhere near the Contents API ceiling",
              max((d / n).stat().st_size for n in shards) < 1_000_000,
              f"largest {max((d / n).stat().st_size for n in shards)} bytes")

        msglog.INDEX_KEEP, keep = 50, msglog.INDEX_KEEP
        msglog._remember(99999, "alert", "cz")
        msglog.flush(); msglog._INDEX = None
        ix = json.loads((d / "index.json").read_text())
        check("the index is capped, not unbounded", len(ix["msgs"]) <= 50, f"{len(ix['msgs'])} entries")
        check("an id evicted from the index still resolves from the local archive",
              msglog.resolve(100) == "c1", str(msglog.resolve(100)))
        msglog.INDEX_KEEP = keep

        # The point of all of it: a reply still lands after `by_message` has aged out.
        st = {"open": {"0x" + "a" * 40: {"id": "c1", "key": "0x" + "a" * 40, "value": 5}},
              "by_message": {}}                          # pruned to nothing, as it is at 600 findings
        k, f = commands.find_by_reply(st, {"reply_to_message": {"message_id": 101}})
        check("a reply to a chart lands on the case after by_message was pruned",
              f is not None and f.get("id") == "c1", str(f))
        k2, f2 = commands.find_by_reply(st, {"reply_to_message": {"message_id": 102}})
        check("a reply to the digest still does NOT act on a case", f2 is None)

        # Both sides are append-only, so a concurrent write merges rather than truncating.
        merged = msglog._merge_lines(b'{"a":1}\n{"b":2}\n', b'{"a":1}\n{"c":3}\n')
        check("a concurrent push merges by line union, it does not overwrite",
              merged.decode().count("\n") == 3 and b'"b":2' in merged and b'"c":3' in merged,
              merged.decode().replace("\n", " "))
    finally:
        msglog.LOCAL, msglog.INDEX, msglog._INDEX = o_local, o_index, o_ix
        msglog.SHARD_MAX = o_shard

    check("the ledger is excluded from the PUBLIC repo",
          "alerts/msglog/" in (ROOT / ".gitignore").read_text())


# ---------------------------------------------------------------- the daily self-test

def t_selftest():
    """notify/selftest.py is the only thing that proves the LIVE path once a day, and
    nothing proved it in turn. These are the properties that stop it becoming the
    outage it exists to catch — plus the fact that decides what it may exercise
    live at all."""
    print("\nthe daily self-test")
    from notify import selftest

    owner = selftest._escalation_owner()
    page = telegram.render(selftest.synthetic_finding("page"))
    check("the self-test's page finding renders as a page, not as a notify",
          "\U0001f6a8" in page and "Needs attention now" in page)
    # "UNASSIGNED" counts as carrying it: a page that admits nobody is named is honest.
    # A page that carries neither has silently dropped the line phase3 §1.2 requires.
    check("the self-test's page copy still carries an escalation contact",
          owner in page or "UNASSIGNED" in page, page[-90:].replace("\n", " "))

    # The live page leg only exercises the twin photo+text path while its banner is
    # longer than a caption; below that the chart rides in the caption and the branch is
    # never executed. Trimming the copy must fail HERE rather than silently downgrade the
    # daily check to the single-message path leg 2 already covers.
    check("the page banner is long enough to force the twin photo+text path",
          len(selftest.PAGE_BANNER) > telegram.CAPTION_MAX,
          f"{len(selftest.PAGE_BANNER)} chars vs caption max {telegram.CAPTION_MAX}")
    for name, text in (("notify", selftest.BANNER), ("page", selftest.PAGE_BANNER)):
        check(f"the {name} self-test message says what it is in its first line",
              "SELF-TEST" in text.split("\n")[0], text.split("\n")[0][:60])
        # A screenshot of a self-test that reads like a live page forwards as one, and
        # deleteMessage recalls nothing that was already forwarded.
        check(f"the {name} self-test message is not shaped like a real alert",
              "Needs attention now" not in text and "Reply to this message" not in text)

    # Isolation. send_pending() writes `incident` into whatever telegram.STATE points at,
    # and an incident left in the LIVE state sends six hours of real alerts silently,
    # under a header nobody received.
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="moca-selftest-t")) / "state.json"
    check("the self-test refuses to send while the live state file is the target",
          selftest._isolation_ok(state_sync.STATE) is False)
    before = (telegram.STATE, telegram.render, telegram.send, telegram._log_out)
    with selftest._isolated(tmp, "\U0001f9ea SELF-TEST", []) as tg:
        inside = (tg.STATE == tmp, selftest._isolation_ok(tmp),
                  tg.render({}) == "\U0001f9ea SELF-TEST")
    check("the self-test writes to its own state file, never the live one",
          inside == (True, True, True), str(inside))
    check("and the real sender is put back afterwards",
          (telegram.STATE, telegram.render, telegram.send, telegram._log_out) == before)
    # Not `_live_state_bytes() == STATE.read_bytes()` — that re-implements the function
    # and compares it to its own body, which is true however broken it is. What has to
    # hold is that the fingerprint MOVES when the real file moves and holds when it
    # does not, because the CLEAN leg's whole claim rests on comparing it before and
    # after a live send.
    keep = state_sync.STATE
    try:
        tmp = pathlib.Path(tempfile.mkdtemp()) / "state.json"
        state_sync.STATE = tmp
        tmp.write_text('{"open": {}}')
        a = selftest._live_state_bytes()
        b = selftest._live_state_bytes()
        tmp.write_text('{"open": {"x": 1}}')
        c = selftest._live_state_bytes()
        tmp.unlink()
        d = selftest._live_state_bytes()
        check("the live-state fingerprint is stable while the file is", a == b and a is not None)
        check("the live-state fingerprint changes when the real file changes", c != a)
        check("a missing live state is not mistaken for an unchanged one", d != c)
    finally:
        state_sync.STATE = keep

    # Sound cannot be proven live without pinging every phone in the group nightly, so
    # the self-test records the ask and refuses it. That is only worth something if the
    # recording and the refusal both work.
    asked, posted = [], []
    o_send = telegram.send
    try:
        telegram.send = lambda text, photo=None, silent=False: (
            posted.append((text, silent)) or {"ok": True, "result": {"message_id": 77}})
        with selftest._isolated(tmp, "\U0001f9ea SELF-TEST", asked) as tg:
            tg.send("x", silent=False)
    finally:
        telegram.send = o_send
    check("the self-test records the sound the send path asked for", asked == [False], str(asked))
    check("...and refuses it, so the group is not pinged nightly",
          bool(posted) and posted[0][1] is True, str(posted))

    # The chart is a REPLY to the text message. Delete the parent first and the chart is
    # left quoting a message that is no longer there.
    deleted, notes = [], []
    o_post, o_send, o_log = telegram._post, telegram.send, telegram._log_out
    try:
        telegram._log_out = lambda *a, **k: None
        telegram.send = lambda text, photo=None, silent=False: (
            notes.append(text) or {"ok": True, "result": {"message_id": 1}})
        telegram._post = lambda m, d=None, files=None: (
            deleted.append(d.get("message_id")) or {"ok": True})
        gone = selftest.delete_messages([120, 121])
        check("both halves of the twin are deleted", sorted(deleted) == [120, 121], str(deleted))
        check("the chart is deleted before the message it replies to",
              deleted == [121, 120], str(deleted))
        check("a clean deletion says nothing in the channel",
              gone is True and not notes, str(notes))

        deleted.clear(); notes.clear()
        telegram._post = lambda m, d=None, files=None: (
            deleted.append(d.get("message_id")) or {"ok": False, "error": "message can't be deleted"})
        stuck = selftest.delete_messages([120, 121])
        check("a refused deletion is explained in the channel, once",
              stuck is False and len(notes) == 1, str(notes)[:80])
        check("the explanation names every message it could not remove",
              bool(notes) and "120" in notes[0] and "121" in notes[0],
              notes[0][:110] if notes else "")
    finally:
        telegram._post, telegram.send, telegram._log_out = o_post, o_send, o_log

    # Why incident mode is proven HERE and never by the live self-test: the header is
    # built inside send_pending() and never passes through telegram.render, so the banner
    # substitution that keeps every other self-test message honest cannot reach it. A live
    # incident leg would post "Incident mode" into the real channel, silent or not.
    o_render = telegram.render
    try:
        telegram.render = lambda _f: selftest.BANNER
        _, sent = run(Bed([_finding(i, signal=str(10 + i)) for i in range(5)]))
    finally:
        telegram.render = o_render
    check("stubbing the renderer does NOT stop an incident header reaching the channel",
          bool(sent) and "Incident mode" in sent[0]["text"],
          sent[0]["text"][:48] if sent else "nothing sent")


# ---------------------------------------------------------------- concurrency

def t_merge():
    print("\nconcurrent writes")
    remote = {"open": {"k1": {"id": "c1", "status": "contained", "status_by": "po",
                              "ack_by": "po", "value": 1},
                       "k2": {"id": "c2", "last_sent": "2026-08-23T01:20:00", "pending_send": False}},
              "telegram_offset": 900, "by_message": {"1": "c1"}}
    local = {"open": {"k1": {"id": "c1", "value": 1, "pending_send": True},
                      "k2": {"id": "c2", "pending_send": True},
                      "k3": {"id": "c3"}},
             "telegram_offset": 880, "by_message": {"2": "c3"}}
    m = state_sync.merge(remote, local)
    check("a human's status survives a concurrent write", m["open"]["k1"].get("status") == "contained")
    check("an alert already sent is not re-armed", m["open"]["k2"].get("pending_send") is False)
    check("a finding only one side has is kept", "k3" in m["open"])
    check("the update offset never goes backwards", m["telegram_offset"] == 900)
    check("the reply map is unioned", set(m["by_message"]) == {"1", "2"})


def t_prune():
    print("\nthe size cap")
    st = {"open": {}}
    for i in range(700):
        st["open"][f"k{i}"] = {"id": f"c{i}", "first_ts": f"2026-08-{1 + i % 21:02d}T00:00:00",
                               "status": ("contained" if i == 0 else None),
                               "ack_by": ("po" if i < 300 else None), "pad": "x" * 200}
    out = state_sync.prune(json.loads(json.dumps(st)))
    check("prune drops down to the count cap", len(out["open"]) <= state_sync.MAX_OPEN,
          f"{len(out['open'])} kept")
    check("a case marked contained is NOT aged out as settled", "k0" in out["open"])
    check("prune records what it retired so it can be announced",
          (out.get("retired_notice") or {}).get("total", 0) > 0, str(out.get("retired_notice")))


def t_shadow():
    print("\nshadow mode")
    from notify import explain

    # A finding in shadow is a digest line: it must not page, must not sound, and
    # must not count toward the loud queue that puts the channel into incident mode.
    sh = _finding(60, tier="digest", signal="INV-10", shadow_of="page",
                  detail="top1=92% n=126", window="6h")
    s, sent = run(Bed([sh]))
    check("a shadowed finding sends nothing loud", sent and all(x["silent"] for x in sent),
          f"{sum(1 for x in sent if not x['silent'])} loud of {len(sent)}")
    check("a shadowed finding is not held as a loud arrival",
          (s.get("loud_held") or 0) == 0, str(s.get("loud_held")))
    check("a shadowed finding still reaches the channel",
          any(explain.SHADOW_HEADING in x["text"] for x in sent))

    s2, _ = run(Bed([_finding(61 + i, tier="digest", signal="INV-10", shadow_of="page",
                              detail=f"top1=9{i}% n=126") for i in range(6)]))
    check("six shadowed findings do not open incident mode", not s2.get("incident"),
          str(bool(s2.get("incident"))))

    body = explain.digest([dict(sh), {"signal": "13", "detail": "score 0.7"}])
    check("the digest names the shadowed check and what it would have been",
          explain.SHADOW_HEADING in body and "would have been a <b>page</b>" in body)
    check("the 'crossed no level' line is not printed over a shadowed finding",
          body.index(explain.SHADOW_HEADING) < body.index("crossed no level"))
    check("a digest with nothing in shadow does not print the shadow heading",
          explain.SHADOW_HEADING not in explain.digest([{"signal": "13", "detail": "score 0.7"}]))

    # ---- the demotion itself, at the detector end of the pipe
    sys.path.insert(0, str(ROOT / "detect"))
    import signals as SG
    check("shadow demotes to digest and remembers the tier it would have had",
          SG.shadow_tier({"shadow_signals": ["INV-10"]}, "INV-10", "page") == ("digest", "page"))
    check("shadow refuses to demote a measured pager",
          SG.shadow_signals({"shadow_signals": ["10", "INV-10"]}) == ({"INV-10"}, ["10"]))
    prev = os.environ.get("THRESHOLDS_JSON")
    os.environ["THRESHOLDS_JSON"] = "{not json"
    try:
        thr = SG.load_thresholds()
    finally:
        if prev is None:
            os.environ.pop("THRESHOLDS_JSON", None)
        else:
            os.environ["THRESHOLDS_JSON"] = prev
    check("a malformed THRESHOLDS_JSON leaves the committed shadow list standing",
          {"INV-10", "INV-11"} <= SG.shadow_signals(thr)[0], str(sorted(SG.shadow_signals(thr)[0])))
    check("a malformed THRESHOLDS_JSON is recorded, not swallowed",
          bool(thr.get("thresholds_override_error")), str(thr.get("thresholds_override_error")))
    ok = SG.load_thresholds()
    check("a run with no override records no override error",
          ok.get("thresholds_override_error") is None)


# ---------------------------------------------------------------- the cluster alert

def t_cluster():
    """S-B is the only signal that names a group instead of a wallet.

    Its message has to say what the group is, name the address a person can act on,
    and name no creator wallet at all: the finding never established that the
    creators share an owner."""
    print("\nthe cluster alert")
    from notify import explain
    check("S-B has a hand-written explanation, not the terse fallback",
          explain.SIGNALS.get("S-B") not in (None, explain.FALLBACK))
    # Built, never written out: the PII gate rejects any real wallet address in a
    # tracked file, and a real collecting wallet in a test fixture is exactly the
    # operational intelligence it exists to keep out of the public repo.
    sink_a, sink_b = "0x" + "d1" * 20, "0x" + "e2" * 20
    sinks = f"{sink_a},{sink_b}"
    f = _finding(1, tier="notify", signal="S-B", key="0ece3bd5df", window="6h",
                 value=0.613, threshold=None,
                 detail=f"cluster6h 2285/3728 m=2 top=1546 s=2 sinks={sinks}",
                 headline=["2 creator wallets paying into the same collecting wallet took "
                           "2,285 of 3,728 equip-sized payouts between them / 6 h"])
    m = explain.measured(f)
    check("the S-B measurement is a sentence, not the raw detail",
          "cluster6h" not in m and "m=2" not in m, m[:90])
    check("the S-B measurement says how many creators are in the group",
          "2 creator wallets" in m, m[:60])
    check("the S-B measurement says what share the group took", "61%" in m, m[:160])
    # The spec's copy told the reader, in all ten of its measured fires, that no member
    # was large enough to alert on its own — while both members were simultaneously
    # firing 10/page, 11/page and S-A/notify in the same slot. That is a mini all-clear
    # about the named wallets, on the busiest page-storm slot of the incident (rule 3).
    check("the S-B measurement does not claim the members are individually quiet",
          "on its own" not in m or "took" in m.split("on its own")[0][-40:], m[:160])
    # And it must not bracket the single-creator line for whoever forwards a screenshot.
    check("the S-B measurement does not bracket the single-creator line",
          "% on its own" not in m, m[:160])
    check("the S-B measurement names the collecting wallet", sink_a in m, m[-90:])
    body = explain.humanise(f)
    check("the S-B alert carries the inferred-from-size hedge", explain.HEDGE in body)
    named = set(re.findall(r"0x[0-9a-fA-F]{40}", body))
    check("the S-B alert names only collecting wallets, never a creator wallet",
          named == {sink_a, sink_b}, str(len(named)))
    check("the S-B alert hands over no threshold",
          "threshold" not in body.lower() and "0.45" not in body)
    g = _finding(2, tier="notify", signal="S-B", key="0ece3bd5df", window="24h", value=41,
                 detail=f"cluster24h 41 m=4 top=12 s=1 sinks={sink_a}",
                 headline=["4 creator wallets paying into the same collecting wallet took "
                           "41 equip-sized payouts between them / 24 h"])
    m2 = explain.measured(g)
    check("the spread-out variant says no member was large on its own",
          "no single one of them took more than 12" in m2, m2[:140])


# ---------------------------------------------------------------- who can pause

def t_owner():
    """Whether a page admits that nobody can stop the money.

    On 20 August 2.4M of the 2.66M MOCA was paid out AFTER the first alert fired:
    authority, not lead time, was the bottleneck. The page then printed
    "Who to ask  Po (interim)" — a placeholder that reads as an answer, which is
    worse than printing nothing (phase3 critic #9)."""
    print("\nwho can pause")
    from notify import explain

    thr = json.loads((ROOT / "detect" / "thresholds.json").read_text())
    ks = thr.get("kill_switch") or {}
    check("thresholds.json carries a kill_switch block", isinstance(thr.get("kill_switch"), dict),
          ",".join(sorted(ks)))
    named = bool(ks.get("owner")) and not explain._PLACEHOLDER.search(str(ks.get("owner")))
    check("a name in the committed file also carries its dated commitment",
          (not named) or all(ks.get(k) for k in explain._KS_REQUIRED), str(ks.get("owner")))

    real = explain._thresholds

    def bed(**kw):
        t2 = dict(thr)
        t2["kill_switch"] = kw
        for name in ("explain", "notify.explain"):   # telegram.py may import either
            if sys.modules.get(name) is not None:
                sys.modules[name]._thresholds = lambda: t2

    def unbed():
        for name in ("explain", "notify.explain"):
            if sys.modules.get(name) is not None:
                sys.modules[name]._thresholds = real

    page = _finding(1, tier="page", signal="10", owner="Po (interim) — @Po_Chu on Telegram",
                    threshold=50, value=148, detail="148/60min", window="60min")
    notif = _finding(2, tier="notify", signal="10n", owner="Po (interim) — @Po_Chu on Telegram",
                     threshold=50, value=60, detail="60/6h", window="6h")
    try:
        bed(owner=None, contact=None, commitment_min=None, agreed_ts=None)
        m = explain.humanise(page)
        check("with nobody named, a page says UNASSIGNED in so many words", "UNASSIGNED" in m)
        check("a page no longer tells Po to ask Po", "Who to ask" not in m)
        check("the UNASSIGNED block says what an empty kill switch cost in August",
              "2.4M" in m and "20 August" in m)
        check("the bot's own contact is labelled as unable to stop a payout",
              "cannot stop a payout" in m)
        check("a notify alert says it too", "UNASSIGNED" in explain.humanise(notif))
        check("the fallback renderer says UNASSIGNED as well",
              "UNASSIGNED" in telegram._render_terse(page))

        bed(owner="Po (interim)", contact="@Po_Chu", commitment_min=30, agreed_ts="2026-08-23")
        check("a placeholder typed into kill_switch.owner is still UNASSIGNED",
              explain.kill_switch() is None and "UNASSIGNED" in explain.humanise(page))

        bed(owner="A Real Person", contact="@handle", commitment_min=None, agreed_ts="2026-08-23")
        check("a kill switch with no agreed response time is still UNASSIGNED",
              explain.kill_switch() is None and "UNASSIGNED" in explain.humanise(page))

        bed(owner="A Real Person", contact="@handle on Telegram", commitment_min=30,
            agreed_ts="2026-08-24T09:00:00+00:00", agreed_by="Po")
        m2 = explain.humanise(page)
        check("a real owner prints the name and the minutes they agreed to",
              "A Real Person" in m2 and "30 minutes" in m2 and "UNASSIGNED" not in m2)
        check("the fallback renderer prints the real owner too",
              "A Real Person" in telegram._render_terse(page))
    finally:
        unbed()

    # The name may arrive in the THRESHOLDS_JSON secret instead of the committed file,
    # because detect/thresholds.json is public and the one person who can stop the
    # money should be named in the group, not on the internet.
    old_env = os.environ.get("THRESHOLDS_JSON")
    os.environ["THRESHOLDS_JSON"] = json.dumps(
        {"kill_switch": {"owner": "A Real Person", "contact": "@handle",
                         "commitment_min": 30, "agreed_ts": "2026-08-24T09:00:00+00:00"}})
    try:
        check("the owner can arrive from the secret, so the public file need not name them",
              (explain.kill_switch() or ("",))[0] == "A Real Person")
        os.environ["THRESHOLDS_JSON"] = "{not json"
        check("a THRESHOLDS_JSON that does not parse leaves the page UNASSIGNED, not wrong",
              explain.kill_switch() is None)
    finally:
        if old_env is None:
            os.environ.pop("THRESHOLDS_JSON", None)
        else:
            os.environ["THRESHOLDS_JSON"] = old_env


# ---------------------------------------------------------------- the council cut list

def t_cut_list():
    """No verb is a dead end, and nothing quiet is a drop (council §5)."""
    print("\nthe retired verbs")
    real_api, real_chat, real_root = commands.api, commands.CHAT, commands.ROOT
    out = []
    typed = int(time.time()) - 3600           # typed an hour ago, read by this poll

    def fresh():
        f = {"id": "c1", "key": "0x" + "a" * 40, "signal": "10", "tier": "page",
             "value": 5.0, "pending_send": True}
        return {"open": {f["key"]: f}, "telegram_offset": 0}, f

    def cmd(text, state, when=typed):
        out.clear()
        commands.handle(state, {"text": text, "message_id": 5, "date": when,
                                "chat": {"id": "-100"}, "from": {"id": "999"}})
        return "\n".join(out)

    try:
        commands.api = lambda method, **p: (out.append(p.get("text", "")) or
                                            {"ok": True, "result": {"message_id": 7}})
        commands.CHAT = lambda: "-100"
        os.environ["TELEGRAM_ACK_USER_IDS"] = "999"

        # (a) the verb is retired; the field it wrote is load-bearing and stays.
        st, f = fresh()
        said = cmd("/ack c1", st)
        check("/ack records a decision instead of muting the case for good",
              f.get("status") == "watching", str(f.get("status")))
        check("/ack says the verb is retired and what it recorded instead",
              "retired" in said and "watching" in said, said[:110])
        check("a case handled by /ack is still listed in /cases",
              "c1" in commands.cases_text(st))
        # The sentence used to claim /ack preserves escalation. It does not prove that:
        # _suppressed() short-circuits on the escalation string before it reads ack_by
        # or status, so it passes identically with or without this change. What it does
        # prove is the sender's half — that an escalation is not swallowed. Whether the
        # detector RAISES one on an /ack'd case is diff_state's job and belongs in
        # test_gate.py. A check whose sentence claims more than its condition is exactly
        # how this suite once asserted a bug as correct.
        check("an escalation is not swallowed by the sender, whatever /ack wrote",
              telegram._suppressed(dict(f, escalation="activity after containment"), {}) is None)
        check("/ack still writes ack_by, which state_sync and prune read",
              f.get("ack_by") == "999", str(f.get("ack_by")))
        check("/ack with no id answers instead of falling silent",
              "Nothing was changed" in cmd("/ack", fresh()[0]))
        check("/help no longer advertises /ack", "/ack" not in cmd("/help", fresh()[0]))

        # (b) /quiet is the council's name for the case-scoped /snooze that exists.
        st, f = fresh()
        said = cmd("/quiet c1 6", st)
        check("/quiet is accepted as a spelling of /snooze",
              f.get("snooze_until") is not None, said[:90])
        check("the quiet window runs from when the person typed it, not from the poll",
              abs(dt.datetime.fromisoformat(f["snooze_until"]).timestamp()
                  - (typed + 6 * 3600)) < 2, str(f.get("snooze_until")))
        check("the confirmation names the time it comes back and says it is held",
              "UTC" in said and "held, not dropped" in said, said[:160])
        check("a quiet case is HELD by the sender, not dropped",
              telegram._suppressed(f, {}) == "snoozed")

        st2, f2 = fresh()
        said2 = cmd("/quiet c1 0.5", st2)
        plain = said2.replace("<b>", "").replace("</b>", "")
        check("a quiet window that expired inside the polling gap is not called quiet",
              "is not quiet" in plain, plain[:170])
        check("and that case really is still sending", telegram._suppressed(f2, {}) is None)

        # No slash command is a dead end.
        check("an unknown command still gets an answer",
              "nothing was changed" in cmd("/freeze c1", fresh()[0]))
        check("/contained with no id and no reply target still gets an answer",
              "nothing was changed" in cmd("/contained", fresh()[0]))
        check("a command aimed at another bot is left alone",
              cmd("/start@SomeOtherBot", fresh()[0]).strip() == "")

        # (c) /status must not read as an all-clear while the detector is blind.
        d = pathlib.Path(tempfile.mkdtemp(prefix="moca-hb-"))
        (d / "heartbeat.json").write_text(json.dumps(
            {"run_ts": dt.datetime.now(dt.UTC).isoformat(),
             "mindset_age_h": 61.0, "lag_blocks": 5400}))
        commands.ROOT = d
        txt = commands.status_text({"open": {}})
        check("/status says what it is blind to instead of reading as an all-clear",
              "degraded" in txt, txt[-160:])
        check("/status still prints no machine jargon",
              "lag" not in txt and "rows" not in txt and "mindset" not in txt)
        (d / "heartbeat.json").write_text(json.dumps(
            {"run_ts": dt.datetime.now(dt.UTC).isoformat(),
             "mindset_age_h": 3.0, "lag_blocks": 12}))
        check("a healthy run adds no degradation line at all",
              "degraded" not in commands.status_text({"open": {}}))
        # Asserted on the object commands.py actually bound, not on the test module's
        # own import — otherwise commands.py could pick up a different module entirely
        # and this check would keep passing.
        check("/status only repeats what the watchdog already announces to this group",
              (commands.watchdog.MINDSET_STALE_H, commands.watchdog.LAG_BLOCKS_MAX) == (48, 900)
              and commands.watchdog is watchdog,
              f"{commands.watchdog.MINDSET_STALE_H} h / {commands.watchdog.LAG_BLOCKS_MAX} blocks")
    finally:
        commands.api, commands.CHAT, commands.ROOT = real_api, real_chat, real_root
        os.environ.pop("TELEGRAM_ACK_USER_IDS", None)
        commands.UNDELIVERED.clear()


def t_digest_example():
    """The digest example is picked by the measurement, not by string length."""
    print("\nthe digest example")
    from notify import explain
    long_string = {"signal": "13", "value": 0.70, "threshold": 0.6, "tier": "digest",
                   "window": "24h", "key": "0x" + "a" * 40, "ts": 100,
                   "detail": "1,234,567 MOCA out, score 0.70"}
    furthest = {"signal": "13", "value": 0.95, "threshold": 0.6, "tier": "digest",
                "window": "24h", "key": "0x" + "b" * 40, "ts": 200,
                "detail": "9 MOCA out, score 0.95"}
    body = explain.digest([long_string, furthest])
    check("the digest example is the finding furthest past its own normal",
          "sent 9 MOCA" in body, body.splitlines()[-3].strip())
    check("the longest detail string no longer decides what a reader is shown",
          "1,234,567" not in body)
    check("the example is offered as an example, not as the largest",
          "one of them:" in body and "e.g." not in body)
    check("a finding with no threshold ranks on its own size",
          explain._extremity({"value": 179.0, "ts": 2})
          > explain._extremity({"value": 170.0, "ts": 3}))
    check("a balance that dropped ranks as far from normal as one that rose",
          explain._extremity({"value": -500.0}) > explain._extremity({"value": 12.0}))
    check("a finding carrying no numbers at all does not crash the picker",
          explain._extremity({"detail": "x"}) == (0.0, 0.0, 0.0))


def main():
    for fn in (t_incident, t_charts, t_dead_copy, t_holding, t_alarm, t_post,
               t_replies, t_reply_time,
               t_reply_delivery, t_dead_copy, t_local_send_guard,
               t_platform_signals_can_speak_twice, t_gate_failure, t_shadow, t_cluster,
               t_owner, t_cut_list, t_selftest,
               t_leaks,
               t_price, t_msglog,
               t_merge, t_prune):
        fn()
    bad = [n for ok, n, _ in RESULTS if not ok]
    print(f"\nnotify: {'FAIL — ' + str(len(bad)) + ' check(s)' if bad else 'OK — all ' + str(len(RESULTS)) + ' checks green'}")
    for n in bad:
        print(f"  FAILED: {n}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())


def test_containment_escalation_is_never_suppressed():
    """A contained case that fires again must page. Suppressing it inverted the one
    command whose purpose is to prove a fix held (fix-round critic #1)."""
    import notify.telegram as T
    f = {"ack_by": "123", "status": "contained",
         "escalation": "activity after containment", "tier": "page"}
    assert T._suppressed(f, {}) is None, "containment escalation must not be suppressed"
    g = {"ack_by": "123", "status": "reported",
         "escalation": "still growing since you reported it", "tier": "notify"}
    assert T._suppressed(g, {}) is None, "growth escalation must not be suppressed"
    h = {"ack_by": "123", "status": "reported"}
    assert T._suppressed(h, {}) == "acked", "a quiet acked case stays quiet"
