#!/usr/bin/env python3
"""Telegram sender for the detection floor.

Tiers:  page (loud, text + chart)   notify (text only)   digest (silent, batched)
State:  alerts/state.json  — written BEFORE sending so a retry never double-sends.
"""
import argparse, json, os, sys, time, urllib.request, urllib.parse, pathlib, datetime as dt

ROOT  = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "alerts" / "state.json"
API   = "https://api.telegram.org/bot{tok}/{m}"
TOK   = lambda: os.environ.get("TELEGRAM_BOT_TOKEN") or _read("telegram_bot_token")
CHAT  = lambda: os.environ.get("TELEGRAM_CHAT_ID")   or _read("telegram_chat_id")

def _read(name):
    p = pathlib.Path.home() / ".moca-ledger" / name
    return p.read_text().strip() if p.exists() else ""

def _post(method, data=None, files=None):
    url = API.format(tok=TOK(), m=method)
    if files:
        boundary = "----mocaledger%d" % time.time_ns()
        body = b""
        for k, v in (data or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        for k, (fn, blob) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                     f"Content-Type: image/png\r\n\r\n").encode() + blob + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data or {}).encode())
    for attempt in range(3):
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            # An exception raised INSIDE this handler is not caught by the sibling
            # `except Exception` below — it escapes _post, escapes send(), and
            # escapes send_pending() after pending_send was already cleared, which
            # loses the alert permanently. Telegram is not always behind Telegram:
            # an edge proxy rate-limiting a burst answers 429 with HTML.
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            if e.code == 429:
                wait = 5
                try:
                    wait = int(json.loads(body).get("parameters", {}).get("retry_after", 5))
                except Exception:
                    print("telegram: 429 with an unparseable body — backing off 5s", file=sys.stderr)
                if attempt == 2:
                    return {"ok": False, "error": "http 429 (rate limited)"}
                time.sleep(min(max(wait, 1), 30))
                continue
            return {"ok": False, "error": f"http {e.code}"}
        except Exception as ex:
            if attempt == 2: return {"ok": False, "error": str(ex)[:80]}
            time.sleep(2)
    return {"ok": False, "error": "retries exhausted"}

CAPTION_MAX = 1024      # Telegram's cap on a photo caption
SEED_ACK = "go-live-seed"   # the 2026-08-22 bootstrap pass, not a human decision


def send(text, photo=None, silent=False):
    """Text first, chart second.

    A caption is cut at 1024 characters and the tail of an alert is the footer —
    which is now the reply instruction, the only way to record a decision. So an
    alert too long to caption is sent as text and the chart follows it as a reply
    to that message; the returned result is always the text message, which is the
    one alerts/state.json maps back to the case.
    """
    have_photo = bool(photo and pathlib.Path(photo).exists())
    if have_photo and len(text) <= CAPTION_MAX:
        r = _post("sendPhoto", {"chat_id": CHAT(), "caption": text, "parse_mode": "HTML",
                                "disable_notification": "true" if silent else "false"},
                  {"photo": (pathlib.Path(photo).name, pathlib.Path(photo).read_bytes())})
        if r.get("ok"): return r
        print(f"sendPhoto failed ({r.get('error')}) — sending as text", file=sys.stderr)
    r = _post("sendMessage", {"chat_id": CHAT(), "text": text[:4096], "parse_mode": "HTML",
                              "disable_web_page_preview": "true",
                              "disable_notification": "true" if silent else "false"})
    if have_photo and len(text) > CAPTION_MAX:
        d = {"chat_id": CHAT(), "caption": "Chart for the alert above.",
             "disable_notification": "true"}          # never a second ping for the same finding
        mid = (r.get("result") or {}).get("message_id")
        if mid: d["reply_to_message_id"] = mid
        p = _post("sendPhoto", d, {"photo": (pathlib.Path(photo).name, pathlib.Path(photo).read_bytes())})
        if not p.get("ok"):
            print(f"chart not delivered for the message above: {p.get('error')}", file=sys.stderr)
        else:
            # The chart is now the most visually prominent object on the page, so it
            # is what people reply to. Hand its id back so the caller can map it to
            # the same case; otherwise the reply gets "I cannot tell which case".
            pid = (p.get("result") or {}).get("message_id")
            if pid:
                r.setdefault("also_message_ids", []).append(pid)
    return r


def _chart_for(f):
    """The chart rides page-tier alerts only (council §5, "charts on notify tier").

    The tier is read off the STATE dict, never off the detector's Finding: an
    escalation to page happens in run.py's diff_state, which rewrites `tier` on the
    stored finding while the Finding object it came from still says notify. Gating
    anywhere upstream of here would send "this is still happening after the fix was
    applied" without the picture its reader needs most.

    incidents/<date>/<hhmm>-<signal>-<key8>/view.png is still written for every
    tier: that folder is the evidence record and has to stay complete. What is cut
    is the CHANNEL. Above CAPTION_MAX the chart is posted as its own message, which
    hands the most forwardable and most reply-attractive object on the page to the
    least urgent tier — for a finding whose recommended action is "review this
    entity; add to watch if the pattern repeats"."""
    return f.get("view_png") if f.get("tier") == "page" else None


def _log_out(r, kind, case_id=None, reply_to=None):
    """Record a delivered message in the durable ledger (notify/msglog.py).

    EVERY send goes through here, not just alerts. `by_message` in alerts/state.json
    only ever held alerts and is pruned to the last 300 entries, so a reply to a
    digest, a notice, a chart or an older alert was unmatchable — and an unmatched
    reply used to be discarded. Never raises: a bookkeeping failure must not cost
    the message it is bookkeeping."""
    try:
        try:
            import msglog
        except ImportError:
            from notify import msglog
        mid = (r.get("result") or {}).get("message_id")
        if mid:
            msglog.record_out(mid, kind, case_id=case_id, reply_to=reply_to)
        for extra in (r.get("also_message_ids") or []):
            # The detached chart: the most reply-attractive object on the page.
            msglog.record_out(extra, "chart", case_id=case_id, reply_to=mid)
    except Exception as ex:
        print(f"msglog: not recorded ({type(ex).__name__}) — reply matching may miss "
              f"this message", file=sys.stderr)


ICON = {"page": "🚨", "notify": "🟠", "digest": "📋", "health": "⏳"}

def render(f):
    """finding dict -> HTML message, written for whoever is awake (see notify/explain.py)"""
    try:
        from explain import humanise
    except ImportError:
        try:
            from notify.explain import humanise
        except ImportError:
            humanise = None
    if humanise:
        try:
            return humanise(f)
        except Exception as ex:
            # Never silent: the run log says why, and the message itself says it fell back.
            print(f"explain.humanise failed for {f.get('id','?')}: {ex!r}", file=sys.stderr)
    return _render_terse(f)


def _render_terse(f):
    """Fallback: the original compact form.

    It announces itself. A silent fallback looks like a normal alert with the
    explanation missing, and the reader cannot tell the difference (council §5).
    """
    i = ICON.get(f.get("tier", "notify"), "🟠")
    head = (f"⚠️ <b>plain-English layer failed —</b> "
            f"{i} <b>{f.get('tier','notify').upper()} · {f.get('signal')}</b>")
    key  = f.get("key", "")
    lines = [head]
    if key: lines.append(f"entity <code>{key}</code>")
    v, t = f.get("value"), f.get("threshold")
    if v is not None: lines.append(f"value <b>{v}</b> vs threshold {t}" + (f" · organic p95 {f['organic_p95']}" if f.get("organic_p95") is not None else ""))
    if f.get("window"): lines.append(f"window {f['window']}")
    for h in (f.get("headline") or [])[:3]: lines.append(f"• {h}")
    if f.get("recommended_action"): lines.append(f"\n➡️ {f['recommended_action']}")
    # The fallback must not be the one renderer still printing a placeholder as if it
    # were an answer. If explain.py is unavailable we say UNASSIGNED: understating who
    # can pause is recoverable, overstating it is the 20 August failure.
    owner_line = "who can pause: UNASSIGNED — nobody has agreed to stop a payout"
    try:
        from explain import pause_terse
        owner_line = pause_terse(f)
    except Exception:
        try:
            from notify.explain import pause_terse
            owner_line = pause_terse(f)
        except Exception as ex:
            print(f"explain.pause_terse unavailable ({ex!r}) — using the literal", file=sys.stderr)
    lines.append(owner_line)
    lines.append(f"\n<i>as of block {f.get('as_of_block','?')}</i>")
    reply = "<b>Reply to this message with:</b>  contained · reported · watching · closed"
    try:                                  # this path exists because explain.py broke
        from explain import REPLY_LINE
        reply = REPLY_LINE
    except Exception:
        try:
            from notify.explain import REPLY_LINE
            reply = REPLY_LINE
        except Exception as ex:
            print(f"explain.REPLY_LINE unavailable ({ex!r}) — using the literal", file=sys.stderr)
    lines.append(reply)
    return "\n".join(lines)

def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {"open": {}, "sent": {}, "telegram_offset": 0, "version": 1}

def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))

def _suppressed(f, s):
    # An escalation always overrides a human status. `contained` in particular exists
    # to make further activity LOUDER: suppressing it inverted the one command whose
    # whole purpose is to prove a fix held.
    if f.get("escalation") in ("activity after containment", "still growing since you reported it"):
        return None
    """Why this finding is not going out right now, or None.

    "acked" is permanent — a person decided it. "snoozed" and "muted" are temporary,
    and send_pending() therefore HOLDS those findings (leaves pending_send True)
    rather than clearing them; clearing turned "quiet for 6 hours" into "never sent"
    with no message, which is the one thing §6.6 forbids. Signal-wide /mute is gone
    (council §5); this still honours any mute left in the state file."""
    # A BOOTSTRAP IS NOT A DECISION. On 2026-08-22 a go-live pass stamped ack_by on 429
    # historical findings so the channel would not re-page the whole August incident on
    # its first run. Sensible — except five of them are keyed on the literal string
    # "platform" (#15, EV, S-Q, S-Q2, #4a), and a finding id is sha256(signal|key), so
    # every FUTURE fire of those signals lands on the same already-acked record. The two
    # platform-wide pagers — the payout-worker ceiling and the equip-velocity spread
    # backstop — were muted for the life of the system by a housekeeping pass, and
    # nothing noticed: not the gate, not the heartbeat, not the daily self-test.
    # A person's ack is still permanent. A seed's is not.
    if f.get("ack_role") or (f.get("ack_by") and f.get("ack_by") != SEED_ACK):
        return "acked"
    sn = f.get("snooze_until")
    if sn and dt.datetime.now(dt.UTC).isoformat() < str(sn): return "snoozed"
    m = (s.get("muted") or {}).get(f.get("signal"))
    if m and dt.datetime.now(dt.UTC).isoformat() < str(m): return "muted"
    return None


def _alert_seq(s, f):
    """How many alerts this channel has now sent about this entity, this one
    included — the footer's '6th alert on this wallet'.

    ONE small integer per finding and no per-entity map: alerts/state.json is
    restored through the GitHub Contents API and dies above 1 MB (critic #2).
    """
    prev = [o.get("alert_seq") or 0 for o in s.get("open", {}).values() if o.get("key") == f.get("key")]
    return max(prev or [0]) + 1


def _chunks(text, limit):
    """Split on line boundaries so an HTML tag is never cut in half."""
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur); cur = ""
        cur += (("\n" if cur else "") + line)
    if cur: out.append(cur)
    return out or [text]


# ------------------------------------------------------------------ incident mode
# During the August incident the detector would have produced ~4 pages per run
# and 1,927 fires in 24 h. Alerting every one of them loudly is how a channel
# gets muted by hour two, and a muted channel is worse than no channel. So above
# a threshold: ONE loud header per run carrying the counts, the money and the
# top wallets, and every individual finding beneath it silent — except the few
# things a person must hear even mid-incident.
#
# Byte budget: state["incident"] is ONE object for the whole incident, not a
# field on each of ~430 findings (alerts/state.json is restored through the
# GitHub Contents API and stops being readable above 1 MB).
INCIDENT_TTL_S = 6 * 3600          # six quiet hours ends an incident
INCIDENT_SHOW = 6                  # same cap the loud path already applies
CASHOUT_KEEP  = 20                 # cash-out destinations remembered per incident
SENDS_KEEP    = 2                  # (sent_at, value) pairs per finding — enough to answer
                                   # "what did the reader see when they typed this?"


def _thresholds():
    try:
        return json.loads((ROOT / "detect" / "thresholds.json").read_text())
    except Exception:
        return {}


def _hedge():
    """The one wording every money figure in this channel carries.

    render() degrades to a terse fallback when explain.py breaks; the header had
    no such net, so a broken explain.py killed send_pending before a single alert
    went out — and only ever during an incident."""
    try:
        from explain import HEDGE
        return HEDGE
    except Exception:
        pass
    try:
        from notify.explain import HEDGE
        return HEDGE
    except Exception as ex:
        print(f"explain.HEDGE unavailable ({ex!r}) — using the literal", file=sys.stderr)
        return "payment type inferred from size on-chain — unconfirmed"


def _wallet_of(f):
    """Dedupe key: the wallet, or the raw finding key for platform-wide findings."""
    k = str(f.get("key") or "").lower()
    return k[5:] if k.startswith("exit:") else k


def _short(key):
    k = str(key or "")
    if k.lower().startswith("exit:"):
        k = k[5:]
    return (k[:10] + "…") if k.lower().startswith("0x") and len(k) > 12 else (k or "?")


def _cashout_min():
    t = _thresholds().get("balance_watch") or {}
    try:
        return float(t.get("cashout_sound_min", 1000))
    except (TypeError, ValueError):
        return 1000.0


def _cashout_addr(f):
    """The exchange-deposit address a MATERIAL amount just arrived at, or None.

    This is the alert with a real freeze window, so the conditions are the ones a
    dust transfer must not be able to satisfy:
      * S-X carrying role exchange_deposit (balance_watch puts it in the headline);
      * an arrival, not a departure — `value` is the signed balance delta, and a
        watched wallet emptying INTO an exchange is not the same event as one
        draining out of it;
      * above cashout_sound_min. `deposit_min` is 1e-06, so without a floor one
        dust deposit is "the first cash-out" and the real one arrives silent.
    The address is returned rather than a bool so the siren is per destination:
    burning it once must not disarm it for every other exchange for hours."""
    if str(f.get("signal")) != "S-X":
        return None
    if not any("exchange_deposit" in str(h) for h in (f.get("headline") or [])):
        return None
    try:
        delta = float(f.get("value"))
    except (TypeError, ValueError):
        return None
    if delta < _cashout_min():
        return None
    return _wallet_of(f) or "?"


def _moca(f):
    try:
        return max(0.0, float(f.get("moca_since")))
    except (TypeError, ValueError):
        return 0.0


def _must_hear(f, inc):
    """The two things a person must hear even mid-incident, whatever their tier.

    These reserve a slot in the batch. Tier-first ordering alone starved the one
    alert with a real freeze window: S-X is emitted at `notify`, so while six page
    findings were pending — which IS a burst — a cash-out never entered the batch
    and the exception the header promises was unreachable."""
    if f.get("escalation") == "activity after containment" or f.get("status") == "contained":
        return "a case marked contained is active again"
    addr = _cashout_addr(f)
    if addr and addr not in (inc.get("cashouts") or []):
        return "first cash-out to this destination in this incident"
    return None


DOUBLED_SOUND = "new alerts have at least doubled since the last run"


def _sound_reason(f, inc):
    """Why this finding is allowed to make a sound inside an incident. None = silent.

    Three of Po's four triggers are properties of the finding and live here. The
    fourth — the rate doubling — is a property of the RUN and is applied once, in
    _sound_plan.

    There used to be a fifth, "the largest payout total this incident has seen",
    which nobody asked for: `t >= 2 * (inc["max_moca"] or 0)` is `t >= 0` before
    anything has sounded, so the first money-bearing finding of every incident
    sounded unconditionally — and the header advertised the invented rule to the
    reader as if it were policy."""
    must = _must_hear(f, inc)
    if must:
        return must
    sig = str(f.get("signal") or "?")
    if sig not in (inc.get("classes") or []):
        return f"first {sig} alert in this incident"
    return None


def _sound_plan(batch, inc, doubled):
    """Decide ONCE per run which findings sound: {finding id: reason}.

    Two reasons this is a plan and not a per-finding call:

    * The header promises "N below will sound". That count used to be computed
      from one pass and the sends from another, while the send loop appended to
      inc["classes"] underneath it — so the header could promise a sound that
      never arrived.
    * "The rate doubling" is Po's fourth trigger and is a run-level fact. It sounds
      ONCE, on the first batch member that has no other reason; firing it on all
      six would be the burst of pings incident mode exists to prevent.
    """
    work = {"classes": list(inc.get("classes") or []),
            "cashouts": list(inc.get("cashouts") or [])}
    plan, doubling_left = {}, bool(doubled)
    for f in batch:
        reason = _sound_reason(f, work)
        if reason is None and doubling_left:
            reason = DOUBLED_SOUND
            doubling_left = False
        if reason:
            plan[str(f.get("id"))] = reason
        # Registered for every batch member, sounding or not: they are all being
        # sent, so the class HAS been shown and must not ring again three lines down.
        sig = str(f.get("signal") or "?")
        if sig not in work["classes"]:
            work["classes"].append(sig)
        addr = _cashout_addr(f)
        if addr and addr not in work["cashouts"]:
            work["cashouts"].append(addr)
    return plan


def _money_totals(findings):
    """(total MOCA, MOCA/h, {wallet: (total, rate)}) deduped by wallet.

    Two signals can fire on the same wallet; adding both totals would report the
    same money twice. Largest total per wallet wins."""
    per = {}
    for f in findings:
        t = _moca(f)
        if t <= 0:
            continue
        try:
            r = float(f.get("rate_per_h") or 0)
        except (TypeError, ValueError):
            r = 0.0
        k = _wallet_of(f)
        if t > per.get(k, (0.0, 0.0))[0]:
            per[k] = (t, r)
    return sum(v[0] for v in per.values()), sum(v[1] for v in per.values()), per


def _incident_state(s, loud, arrivals, now):
    """(inc, on, doubled). Opens, carries or expires the one incident object.

    An incident ends on the TTL — six hours with nothing loud — not on the first
    quiet run. A run with nothing pending is routine mid-burst (findings only
    re-fire when they grow), and ending there un-muted the channel and re-armed
    every signal class to ring again as "first in this incident".

    `loud` is the queue, not a count, because the run that OPENS an incident has
    to seed `classes` from it. It used to open with `classes: []`, so every
    distinct signal already waiting counted as "first of its class" and the
    opening run sent the header plus one loud ping per signal — 7 loud messages
    on an 8-finding batch. The opening run is precisely the moment the channel
    gets muted by a person, and it was the worst run in the incident. The first
    wave IS the incident; the header is its ping. Only a class that turns up in a
    LATER run is news."""
    thr_min = int(_thresholds().get("incident_mode_min", 3) or 3)
    inc = s.get("incident") or {}
    expired = bool(inc) and now - float(inc.get("last_ts") or 0) > INCIDENT_TTL_S
    if expired:
        inc = {}
    prev = int(s.get("arrivals_prev") or 0)
    doubled = prev > 0 and arrivals >= 2 * prev
    on = len(loud) > thr_min or bool(inc)
    if on and not inc:
        # Seeded from the whole queue, not just the six that will be shown: the held
        # remainder is the same wave, and it arrives under its own loud header later.
        # `cashouts` is NOT seeded — a first cash-out to a destination is one of the
        # things Po said a person must hear even in the opening minute.
        inc = {"started": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
               "runs": 0, "cashouts": [], "last_ts": now,
               "classes": sorted({str(f.get("signal") or "?") for f in loud})}
    return inc, on, doubled


def _since_words(started):
    """'3 h 20 min' since the incident opened, for the header's money window."""
    try:
        t0 = dt.datetime.fromisoformat(str(started))
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=dt.UTC)
        mins = max(0, int((dt.datetime.now(dt.UTC) - t0).total_seconds() // 60))
    except Exception:
        return None, None
    if mins < 90:
        return f"{mins} min", mins / 60.0
    return f"{mins // 60} h {mins % 60:02d} min", mins / 60.0


def _incident_header(loud, batch, inc, arrivals, arrivals_prev, held_prev, doubled, plan):
    """The single loud message. Counts, money, top wallets, and the limits of all
    of it — a reader who only ever sees this message must not be misled by it."""
    HEDGE = _hedge()
    total, rate, per = _money_totals(loud)
    started = inc.get("started")
    since, hours = _since_words(started)
    n_shown = len(batch)
    tiers = {}
    for f in loud:
        tiers[f.get("tier") or "?"] = tiers.get(f.get("tier") or "?", 0) + 1
    tier_mix = ", ".join(f"{v} {k}" for k, v in sorted(tiers.items()))

    L = [f"🚨 <b>Incident mode — {arrivals} new alert(s) this run</b>",
         "<i>" + f"{len(loud)} waiting in total ({tier_mix})"
         + (f"; {held_prev} carried over from earlier runs" if held_prev > 0 else "")
         + (f" · started {str(started)[11:16]} UTC, {since} ago" if since else "") + "</i>", ""]

    if total > 0:
        usd_note = "price unavailable"
        rd = None
        try:
            sys.path.insert(0, str(ROOT / "detect"))
            import price
            v, usd_note = price.usd(total)
            d = price.fmt_usd(v)
            rd = price.fmt_usd(price.usd(rate)[0]) if rate > 0 else None
        except Exception:
            d = None
        money = f"~{total:,.0f} MOCA" + (f" (~{d})" if d else "")
        money += f" across {len(per)} wallet(s)"
        if rate > 0:
            money += f", about {rate:,.0f} MOCA/h" + (f" (~{rd}/h)" if rd else "")
        L += [f"<b>Money</b>  {money}",
              "<i>Treasury payouts to these wallets only — money moving between wallets, "
              "or into a collector nobody pays from the Treasury, is NOT in this figure. "
              "Each wallet is counted from when its own alert first fired, so this is not "
              f"one clean window. {HEDGE} · {usd_note}.</i>", ""]

    top = sorted(per.items(), key=lambda kv: -kv[1][0])[:3]
    if top:
        L += ["<b>Top by Treasury payouts</b>  "
              + " · ".join(f"<code>{_short(k)}</code> ~{v[0]:,.0f} MOCA" for k, v in top), ""]
    else:
        seen, names = set(), []
        for f in sorted(loud, key=lambda x: 0 if x.get("tier") == "page" else 1):
            k = _wallet_of(f)
            if k in seen:
                continue
            seen.add(k); names.append(f"<code>{_short(k)}</code> ({f.get('signal')})")
            if len(names) == 3:
                break
        if names:
            L += ["<b>Most active</b>  " + " · ".join(names)
                  + "  <i>(no Treasury-payout total for these: what they measure is a share "
                    "or a count, and money that did not come from the Treasury is not "
                    "totalled here — it can still be large)</i>", ""]

    if doubled:
        L += [f"<b>Volume</b>  {arrivals} new alert(s) this run against {arrivals_prev} "
              f"last run — arrivals have at least doubled.", ""]

    held = len(loud) - n_shown
    L += [f"Showing {n_shown} of {len(loud)} below"
          + (f"; {held} held — they send in later runs, newest last." if held > 0 else "."),
          "The alerts below are <b>silent</b> unless marked otherwise — this message is the "
          "ping. Sound is kept for four things: a signal type not yet seen in this incident, "
          "a first cash-out to a destination, a case you marked contained firing again, and "
          "the rate of new alerts doubling."
          + (f" {len(plan)} below will sound." if plan else ""), "",
          "<b>This does not see everything.</b> Some ways of moving value produce no alert at "
          "all, and what is shown can be several minutes behind. Silence here is not an "
          "all-clear. Nothing has been paused or blocked — this bot only informs.", "",
          "Reply to any alert below with <b>reported</b> · <b>contained</b> · <b>watching</b> · "
          "<b>closed</b>. Replying to <i>this</i> message will not match a case."]
    return "\n".join(L)


def send_pending():
    """Send findings marked pending in alerts/state.json (written by detect/run.py)."""
    s = load_state()
    for f in s.get("open", {}).values():
        if not f.get("pending_send"):
            continue
        why = _suppressed(f, s)
        if why == "acked":
            # A person handled it. That is permanent by their decision, not ours.
            f["pending_send"] = False; f["suppressed"] = why
        elif why:
            # Snoozed. HELD, not dropped: pending_send stays True so it sends when
            # the snooze expires. Clearing it turned "quiet for 6 h" into "never"
            # and violated the rule that no finding is dropped without saying so.
            f["suppressed"] = why
    pending = [f for f in s.get("open", {}).values()
               if f.get("pending_send") and not _suppressed(f, s)]
    digest = [f for f in pending if f.get("tier") == "digest"]
    loud = [f for f in pending if f.get("tier") != "digest"]

    # ---- arrivals vs backlog. len(loud) is the QUEUE; the number a reader uses to
    # judge scale must be what turned up this run, or a draining queue reads as an
    # accelerating attack and a real slowdown is invisible.
    held_prev = int(s.get("loud_held") or 0)
    arrivals = max(0, len(loud) - held_prev)
    arrivals_prev = int(s.get("arrivals_prev") or 0)

    now = time.time()
    inc, incident_on, doubled = _incident_state(s, loud, arrivals, now)
    if not incident_on and s.get("incident"):
        prev_inc = s.pop("incident")
        print("incident: over (six hours with nothing loud)")
        _log_out(send("🔕 <b>Incident mode off.</b> Alerts are loud again.\n"
             "<i>This is not an all-clear — only a person says that. It means six hours "
             "passed with nothing new loud enough to alert on. Send <code>/cases</code> "
             "for what is still open.</i>", silent=True), "notice")

    # ---- anything the state layer dropped must be SAID, not only logged (§6.6)
    rn = s.get("retired_notice")
    if rn:
        s.pop("retired_notice", None); save_state(s)
        _log_out(send(f"⚠️ <b>{rn.get('unacked', 0)} unacknowledged case(s) were aged out of my memory.</b>\n"
             f"The case list is at its size limit, so the oldest {rn.get('total', 0)} finding(s) "
             f"were retired to keep the detector able to restore its state at all. They are gone "
             f"from <code>/cases</code> and cannot be replied to. The chain data behind them is "
             f"still in the repo.", silent=False), "notice")

    if not pending:
        s["loud_held"] = 0
        s["arrivals_prev"] = arrivals
        if incident_on:
            s["incident"] = inc          # carried by the TTL, not by this run being busy
        s["last_run_ts"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        save_state(s)
        print("nothing pending"); return 0

    # ---- what gets a slot. Tier-first alone starved the one alert with a real
    # freeze window: S-X is `notify`, so during a burst (>= 6 pages pending, which
    # IS the definition of a burst) a cash-out never entered the batch and the
    # sound exception the header promises was unreachable. Must-sound findings take
    # their slot first, whatever their tier. first_ts breaks ties so the order does
    # not silently flip when prune() rewrites the key order of `open`.
    def _rank(f):
        must = 0 if (incident_on and _must_hear(f, inc)) else 1
        return (must, 0 if f.get("tier") == "page" else 1, str(f.get("first_ts") or ""))
    batch = sorted(loud, key=_rank)[:INCIDENT_SHOW]

    # ---- who sounds. Decided once, before the header, so the header's promise and
    # the sends below are the same decision (see _sound_plan).
    plan = _sound_plan(batch, inc, doubled) if incident_on else {}

    # ---- the one loud message. If it does NOT deliver, the findings below stay
    # loud: silencing them behind a header nobody received would mute the run.
    header_ok = False
    if incident_on and batch:
        header = _incident_header(loud, batch, inc, arrivals, arrivals_prev, held_prev, doubled, plan)
        s["incident"] = inc; save_state(s)               # commit intent BEFORE sending
        header_ok = True
        err = None
        for i, chunk in enumerate(_chunks(header, 3800)):
            r = send(chunk, silent=bool(i))
            _log_out(r, "notice")                        # a reply to the header must be
            if not r.get("ok"):                          # answerable. EVERY chunk, not just the last:
                header_ok = False                        # chunk 1 carries the counts and the
                err = str(r.get("error"))[:80]           # money, and a lost chunk 1 with a
        inc["header_ok"] = header_ok                     # delivered chunk 2 would mute the run
        inc["header_error"] = err
        save_state(s)
        print(f"incident: header ok={header_ok} arrivals={arrivals} queue={len(loud)} "
              f"shown={len(batch)} doubled={doubled}")
        if not header_ok:
            print("incident: header undelivered — findings below stay loud")

    for f in batch:
        reason = plan.get(str(f.get("id"))) if (incident_on and header_ok) else None
        silent = bool(incident_on and header_ok and not reason)
        f["pending_send"] = False; f["last_sent"] = dt.datetime.now(dt.UTC).isoformat()
        f["alert_seq"] = _alert_seq(s, f)                   # footer: "6th alert on this wallet"
        save_state(s)                                       # commit intent BEFORE sending
        body = render(f)
        if reason:
            body += f"\n<i>🔔 Sounding despite incident mode: {reason}.</i>"
        elif silent:
            # A silenced page renders identically to a live one, and a screenshot of
            # it forwards as a live page. Say which it is.
            body += ("\n<i>🔕 Sent silently under incident mode — the header above is this "
                     "run's ping.</i>")
        r = send(body, photo=_chart_for(f), silent=silent)
        f["send_ok"] = bool(r.get("ok")); f["send_error"] = None if r.get("ok") else str(r.get("error"))[:80]
        if r.get("ok"):
            # What the reader was actually looking at, and when. A person replies to
            # an ALERT, not to the state file, and their reply is read up to an hour
            # later; stamping the baseline from the value at poll time folded the
            # growth they never saw into the number their "contained" is judged
            # against (fix-round critic #5). Two entries, newest first: a re-fire can
            # land between the reply being typed and the reply being read.
            f["sends"] = ([[int(time.time()), f.get("value")]] + (f.get("sends") or []))[:SENDS_KEEP]
        _log_out(r, "alert", case_id=f.get("id"))
        mid = (r.get("result") or {}).get("message_id")
        if mid:
            f["tg_message_id"] = mid
            s.setdefault("by_message", {})[str(mid)] = f.get("id")   # fast path; pruned to 300
        for extra in (r.get("also_message_ids") or []):              # the detached chart
            s.setdefault("by_message", {})[str(extra)] = f.get("id")
        if incident_on and r.get("ok"):
            sig = str(f.get("signal") or "?")
            if sig not in (inc.get("classes") or []):
                inc.setdefault("classes", []).append(sig)
            addr = _cashout_addr(f)
            if addr:
                co = inc.setdefault("cashouts", [])
                if addr not in co:
                    co.append(addr)
                    del co[:-CASHOUT_KEEP]               # bounded: one incident object, ~430 findings
        save_state(s)
        print(f["tier"], f.get("signal"), "->", r.get("ok"), "silent" if silent else "loud")

    if incident_on:
        inc["runs"] = int(inc.get("runs") or 0) + 1
        if loud:
            inc["last_ts"] = now          # the TTL measures QUIET, so only loud runs refresh it
        s["incident"] = inc
    still_pending = [f for f in s.get("open", {}).values()
                     if f.get("pending_send") and f.get("tier") != "digest" and not _suppressed(f, s)]
    s["loud_held"] = len(still_pending)
    s["arrivals_prev"] = arrivals
    s["last_run_ts"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    save_state(s)

    # ---- the send-failure alarm reports THIS run. Keyed on every finding ever
    # marked send_ok False it latched forever: a failed send leaves pending_send
    # cleared, so it is never retried, so send_ok stays False, so the flag never
    # cleared and no later failure was ever announced.
    failed_now = [f for f in batch if f.get("send_ok") is False]
    if failed_now and not s.get("send_failure_alarmed"):
        s["send_failure_alarmed"] = True; save_state(s)
        a = send(f"🔴 <b>{len(failed_now)} alert(s) failed to send this run</b> — check the run log; "
                 f"the findings are in the repo index", silent=False)
        _log_out(a, "notice")
        if not a.get("ok"):
            print(f"telegram: the send-failure alarm ITSELF failed ({a.get('error')})", file=sys.stderr)
            s["send_failure_alarmed"] = False          # so the next failure still tries
            save_state(s)
    elif not failed_now and s.get("send_failure_alarmed"):
        s["send_failure_alarmed"] = False; save_state(s)

    if digest:
        for f in digest: f["pending_send"] = False; f["last_sent"] = dt.datetime.now(dt.UTC).isoformat()
        save_state(s)
        try:
            from explain import digest as fmt_digest
        except ImportError:
            from notify.explain import digest as fmt_digest
        body = fmt_digest(digest)
        ok = True
        for chunk in _chunks(body, 3800):        # never truncate mid-tag: HTML would 400
            r = send(chunk, silent=True)
            _log_out(r, "digest")                # so "that was the digest, not a case" is sayable
            if not r.get("ok"):                  # every chunk, not just the last
                ok = False
        if not ok:
            for f in digest:                     # not delivered -> keep it pending
                f["pending_send"] = True
            print("digest: send failed, left pending")
        save_state(s)
    return 0

def gate_failed(url):
    """The alerting behaviour gate went red and we sent anyway. Say so, once per 6 h.

    The gate no longer blocks the send path (crawl.yml): a behaviour regression used to
    stop Notify, commands.py and the state push every ten minutes until somebody opened
    the Actions tab. Sending past it is the better trade — but only if the channel is
    told, or "quiet and nobody knows why" has simply moved one step upstream."""
    s = load_state(); last = s.get("last_gate_post", 0); now = time.time()
    if now - last < 6 * 3600:
        print("gate-failure post deduped"); return 0
    s["last_gate_post"] = now; save_state(s)
    _log_out(send(
        "\u26a0\ufe0f <b>My own behaviour checks are failing.</b>\n"
        "Alerts are still being sent — I did not go quiet — but something I rely on to "
        "decide <i>how</i> to alert is not behaving as it was written to. Treat what "
        "arrives from me as less trustworthy than usual until this clears, and do not "
        "read silence as an all-clear.\n"
        f"{url}", silent=False), "notice")
    return 0


# ------------------------------------------------------------------ proof of life
# Po's ask, and it runs straight into rule 3 (never say all-clear). A channel that is
# silent for days is indistinguishable from a channel that is dead, and this system's
# defining failure mode is "quiet, and nobody knows why". So once a day it says it is
# running — and the whole design problem is saying that without it becoming a
# reassurance somebody screenshots.
#
# Three things keep it honest:
#   * it carries MOVING NUMBERS (block height, blocks since yesterday, runs). A message
#     that says "all good" proves nothing; a block height that advanced proves the
#     pipeline ran end to end. If the numbers stop moving the message says so itself.
#   * it never claims nothing happened — only that nothing crossed a level, in the same
#     words the digest already uses.
#   * it REFUSES to be reassuring when the detector is running but blind. Alive-and-blind
#     is worse than silent, because silence is at least honest.
HEARTBEAT_MAX_LAG = 900       # blocks behind tip before "running" stops meaning "seeing"


def _hb_doc():
    try:
        return json.loads((ROOT / "heartbeat.json").read_text())
    except Exception:
        return {}


def _blind_reasons(hb):
    """Why a 'still watching' message would be a lie right now. Empty = it is true."""
    out = []
    try:
        lag = int(hb.get("lag_blocks") or 0)
        if lag > HEARTBEAT_MAX_LAG:
            out.append(f"I am {lag:,} blocks behind the chain tip, so I am not seeing recent activity")
    except (TypeError, ValueError):
        pass
    # Ask the DETECTOR whether it is blind, do not re-decide it here. This carried its
    # own 48-hour threshold copied from the watchdog, while the detector's own line is
    # stale_after_h = 24 (signals/__init__.py) — so at 29 h the detector had already
    # declared "hashed-stale" and FALLEN BACK, and this message still posted a green
    # tick saying "Still watching". Two thresholds for one question is how a proof of
    # life ends up reassuring about a system that is running blind. There is now one.
    if str(hb.get("mindset_source") or "").endswith("stale"):
        age = hb.get("mindset_age_h")
        out.append(f"my list of which wallets are creators is stale"
                   + (f" ({age:.0f} h old)" if isinstance(age, (int, float)) else "")
                   + " and I have fallen back to reading it from the chain alone, so a "
                     "creator I have not seen pay out before may not be recognised")
    if hb.get("detect_ok") is False:
        out.append("my last detection pass did not finish")
    if hb.get("thresholds_override_error"):
        out.append("a threshold override was rejected, so I am running on committed defaults")
    return out


def heartbeat(force=False):
    """One proof-of-life message per UTC day. Silent. Never an all-clear."""
    s = load_state()
    hb = _hb_doc()
    today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    if not force and s.get("last_heartbeat_day") == today:
        print("heartbeat: already sent today"); return 0

    block = hb.get("ledger_last") or "?"
    rows = int(hb.get("rows_total") or 0)
    prev_rows = int(s.get("last_heartbeat_rows") or 0)
    scanned = rows - prev_rows if prev_rows else None
    since = s.get("last_heartbeat_day")
    open_f = hb.get("open_findings") or {}
    fires = hb.get("fires_last_24h_total")

    live = [f for f in (s.get("open") or {}).values()
            if f.get("status") in ("reported", "contained", "watching")]
    blind = _blind_reasons(hb)

    L = ["\U0001f7e2 <b>Still watching.</b>" if not blind
         else "\u26a0\ufe0f <b>Running, but not seeing properly.</b>"]
    L.append(f"<i>Chain read up to {block} UTC"
             + (f" · {scanned:,} new rows since yesterday" if scanned and scanned > 0 else "")
             + (f" · {fires} thing(s) crossed a level in the last 24 h" if fires is not None else "")
             + "</i>")
    if blind:
        L += ["", "<b>Why that matters:</b>"] + [f"• {b}" for b in blind]
        L += ["", "Until that clears, treat quiet from me as unknown rather than quiet."]
    if live:
        L += ["", f"{len(live)} case(s) you have marked and not closed. <code>/cases</code> for the list."]
    L += ["", "<i>This is not an all-clear. It means the pipeline ran and wrote a number that "
              "moved — nothing more. Some ways of moving value produce no alert at all, and I "
              "cannot pause or block anything.</i>"]

    r = send("\n".join(L), silent=True)
    _log_out(r, "health")
    if r.get("ok"):
        s["last_heartbeat_day"] = today
        s["last_heartbeat_rows"] = rows
        save_state(s)
        print(f"heartbeat: sent ({'blind' if blind else 'ok'}, {scanned if scanned else '?'} new rows)")
    else:
        # Never silent about the thing whose entire job is to prove we are not silent.
        print(f"heartbeat: NOT delivered ({r.get('error')}) — the channel has no proof of life "
              f"today", file=sys.stderr)
        return 3
    return 0


def failure(url):
    s = load_state(); last = s.get("last_failure_post", 0); now = time.time()
    if now - last < 6 * 3600 and s.get("last_run_ok", True):
        print("failure post deduped"); return 0
    s["last_failure_post"] = now; s["last_run_ok"] = False; save_state(s)
    _log_out(send(f"🔴 <b>detector run failed</b>\n{url}", silent=False), "notice"); return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-pending", action="store_true")
    ap.add_argument("--failure", metavar="URL")
    ap.add_argument("--gate-failed", metavar="URL", dest="gate_failed")
    ap.add_argument("--heartbeat", action="store_true", help="once-a-day proof of life")
    ap.add_argument("--force", action="store_true", help="with --heartbeat: send even if today already had one")
    ap.add_argument("--test", metavar="TEXT")
    a = ap.parse_args()
    if a.failure: sys.exit(failure(a.failure))
    if a.gate_failed: sys.exit(gate_failed(a.gate_failed))
    if a.heartbeat:   sys.exit(heartbeat(force=a.force))
    if a.test:
        r = send(a.test); _log_out(r, "test"); print(r); sys.exit(0)
    sys.exit(send_pending())
