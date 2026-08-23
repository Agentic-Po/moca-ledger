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

from notify import telegram, commands, state_sync, msglog   # noqa: E402

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
            SENT.append({"text": text, "silent": silent})
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


def main():
    for fn in (t_incident, t_holding, t_alarm, t_post, t_replies, t_reply_time,
               t_reply_delivery, t_dead_copy, t_gate_failure, t_shadow, t_leaks,
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
