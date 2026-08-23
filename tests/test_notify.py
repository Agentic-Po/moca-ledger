#!/usr/bin/env python3
"""Behaviour tests for the alerting layer — the parts that decide whether the   # pii-ok
channel goes quiet, lies, or leaks.

Every check here corresponds to a defect that was found by review rather than by a
run, which is exactly the class of defect that stays invisible until an incident.
Nothing here touches the real alerts/state.json and nothing sends to Telegram: the
sender is stubbed and every state file is a temporary one.

Usage:  python3 tests/test_notify.py        (exit 1 on any failure)
"""
import json, pathlib, re, sys, tempfile, time, urllib.error, io, datetime as dt

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notify import telegram, commands, state_sync           # noqa: E402

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


def main():
    for fn in (t_incident, t_holding, t_alarm, t_post, t_replies, t_leaks, t_merge, t_prune):
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
