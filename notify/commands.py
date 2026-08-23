#!/usr/bin/env python3
"""Telegram command handler — runs once per slot inside the workflow.

Reads new updates with getUpdates (offset persisted in alerts/state.json), applies
the case verbs (/reported /contained /watching /close /reopen), /snooze (also spelled
/quiet), /cases, /status and /help, and replies in-thread. Privacy mode may stay ON:
commands are always delivered to bots.

/ack and /mute are RETIRED verbs (council §5) that still ANSWER. Retiring a verb
cannot mean deleting the branch: somebody types it from muscle memory at 03:00, and
a command that reaches no branch falls off the end of handle() into silence.

Only user ids listed in TELEGRAM_ACK_USER_IDS may change state.
"""
import json, os, pathlib, re, sys, urllib.parse, urllib.request, datetime as dt

ROOT  = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "alerts" / "state.json"
HOME  = pathlib.Path.home() / ".moca-ledger"

try:                              # the durable message ledger; see notify/msglog.py
    import msglog
except ImportError:
    from notify import msglog

try:                              # only for its two degradation thresholds
    from notify import watchdog    # the package form first: a same-named PyPI
except ImportError:               # package can never satisfy it, and a shadowed
    import watchdog               # module would silently change the thresholds

def _cfg(env, fname):
    return os.environ.get(env) or ((HOME / fname).read_text().strip() if (HOME / fname).exists() else "")

TOK  = lambda: _cfg("TELEGRAM_BOT_TOKEN", "telegram_bot_token")
CHAT = lambda: _cfg("TELEGRAM_CHAT_ID",   "telegram_chat_id")
ACK  = lambda: {x.strip() for x in (_cfg("TELEGRAM_ACK_USER_IDS", "telegram_ack_user_ids") or "").replace(",", " ").split() if x.strip()}

def api(method, **params):
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOK()}/{method}",
                                 data=urllib.parse.urlencode(params).encode())
    try:    return json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as e: return {"ok": False, "error": str(e)[:80]}

def _chunks(text, limit=3800):
    """Split on line boundaries so an HTML tag is never cut in half.

    `text[:4000]` under parse_mode=HTML cut /cases mid-<b>; Telegram answers 400
    "can't parse entities", api() swallowed it and the reader got nothing at all."""
    out, cur = [], ""
    for line in str(text).split("\n"):
        while len(line) > limit:                       # a single monstrous line
            out.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur); cur = ""
        cur += (("\n" if cur else "") + line)
    if cur: out.append(cur)
    return out or [""]


UNDELIVERED = []      # replies Telegram refused this run — see main()

# What happened to the message currently being handled, for the ledger. Filled at
# each decision point in handle() and read once, in main(), so that EVERY update is
# recorded — matched or not, understood or not. An update that reaches none of these
# branches is still written, as "seen": the rule is that nothing a person sends is
# dropped without a row saying so.
OUTCOME = {}


def reply(text, to=None):
    """Send, in as many messages as it takes, and report whether it arrived.

    Every one of the ~15 call sites used to throw this result away, so a reply that
    404'd or 400'd left handle() returning 0, main() returning 0 and the run GREEN,
    while the person who typed it got nothing at all (fix-round critic #6). Recording
    it here rather than at each call site is deliberate: a new call site cannot forget.
    Callers that want to branch still can — the return value is unchanged."""
    ok = True
    for chunk in _chunks(text):
        p = {"chat_id": CHAT(), "text": chunk, "parse_mode": "HTML", "disable_notification": "true"}
        if to: p["reply_to_message_id"] = to
        r = api("sendMessage", **p)
        if not r.get("ok"):
            ok = False
            print(f"commands: reply NOT delivered ({r.get('error')}) — "
                  f"the person who asked got nothing", file=sys.stderr)
        else:
            # People reply to my confirmations too. Recorded so "I cannot tell which
            # case" can become "that was one of my own confirmations".
            msglog.record_out((r.get("result") or {}).get("message_id"), "confirm")
    if not ok:
        UNDELIVERED.append({"reply_to": to, "starts": str(text)[:60]})
    return ok

def _deny(uid, mid):
    """Fail closed, but never a dead end.

    TELEGRAM_ACK_USER_IDS is a repo secret only an admin can edit, so a colleague who
    types `contained` and gets a bare "not authorised" has no route in at all. Name who
    can add them, and hand them the one value that needs adding — their own numeric id,
    which they otherwise have no way to look up."""
    return reply(
        "\U0001f512 <b>Not authorised — nothing was changed.</b>\n"
        "Only people on the on-call list can change a case. Reading is open to everyone: "
        "<code>/cases</code> and <code>/status</code> still work.\n\n"
        f"To be added, ask <b>{_owner_name()}</b>. Your Telegram user id is "
        f"<code>{uid or 'unknown'}</code> \u2014 that is all they need; adding you is one "
        "line, in the repo README under \u201cWho can change a case\u201d.", mid)


def _owner_name():
    try:
        return json.loads((ROOT / "detect" / "thresholds.json").read_text()).get("escalation_owner", "the on-call")
    except Exception:
        return "the on-call"


def _esc(x):
    """A human's own words come back out inside parse_mode=HTML. A note containing
    `<` produced a 400 that api() swallowed, and /cases went permanently dead with
    no error anywhere; a note containing an <a href> injected a clickable link into
    every future rendering."""
    return (str(x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _log_in(u):
    """Write one row for this update, whatever became of it. Never raises."""
    try:
        if OUTCOME.get("redact"):
            # A message from a chat this bot does not serve. It is recorded — silence
            # is what this ledger exists to prevent — but a stranger's words and the
            # chat they came from are not ours to keep.
            m = dict((u or {}).get("message") or {})
            m.pop("text", None); m.pop("chat", None); m.pop("reply_to_message", None)
            u = dict(u or {}, message=m)
        msglog.record_in(u, resolved_case=OUTCOME.get("case"),
                         outcome=OUTCOME.get("outcome") or "seen",
                         action=OUTCOME.get("action"))
    except Exception as e:
        print(f"commands: msglog.record_in failed ({type(e).__name__})", file=sys.stderr)


def load(): return json.loads(STATE.read_text()) if STATE.exists() else {"open": {}, "telegram_offset": 0}
def save(s): STATE.write_text(json.dumps(s, indent=1))

def find_by_reply(s, m):
    """Which case is this message a reply to? Telegram delivers replies to the bot
    even with privacy mode on, so replying to an alert is the natural way to act on it.

    Two layers, in order of cost: `by_message` in the state file is the fast path but
    is pruned to the last 300 entries and only ever held alerts; the message ledger
    holds every message either way sent, follows the parent chain (a reply to a chart
    or to a tier-2 detail resolves to the alert they hang off), and is not pruned."""
    r = m.get("reply_to_message") or {}
    mid = r.get("message_id")
    if not mid: return None, None
    fid = (s.get("by_message") or {}).get(str(mid))
    if not fid:
        try:
            fid = msglog.resolve(mid)
        except Exception as e:
            print(f"commands: msglog.resolve failed ({type(e).__name__})", file=sys.stderr)
    if fid: return find(s, fid)
    return None, None


PREFIX_MIN = 8      # `/close 0x` closed the first case in dict order (council §5)


def find(s, ident):
    """Exact id or key, else an UNAMBIGUOUS entity prefix of at least PREFIX_MIN.

    The old fallback matched any prefix of any length and returned the first hit in
    dict order, so `/close 0x` closed an arbitrary case — with a confirmation naming
    an id the sender never typed."""
    ident = (ident or "").strip().lower()
    for k, f in (s.get("open") or {}).items():
        if str(f.get("id", "")).lower() == ident or k.lower() == ident: return k, f
    if len(ident) < PREFIX_MIN:
        return None, None
    hits = [(k, f) for k, f in (s.get("open") or {}).items()
            if str(f.get("key", "")).lower().startswith(ident)]
    return hits[0] if len(hits) == 1 else (None, None)

STATUSES = {
    "reported":  ("📣", "team informed, action pending"),
    "contained": ("🛡", "a fix was applied"),
    "closed":    ("✅", "resolved"),
    "watching":  ("👀", "seen, still watching"),
}


def _sent_at(when):
    """The moment the person actually typed it. `when` is Telegram's `message.date`.

    Replies are read by a poll that runs every ten minutes and has been measured at
    p95 56 min. Stamping the decision from the poll instead of from the message
    credited the detector with everything that happened in between: run.py gates the
    "still growing since you contained it" escalation on 1.5x of `value_at_status`,
    so a baseline inflated by an unseen hour needs 1.5x of an already-grown number
    before it fires, and explain.py then tells the reader "about the same level as
    when you contained it" about a period they never saw (fix-round critic #5)."""
    try:
        return dt.datetime.fromtimestamp(int(when), dt.UTC)
    except (TypeError, ValueError):
        return dt.datetime.now(dt.UTC)


def _value_as_of(f, at):
    """The value the alert this person was looking at actually carried.

    telegram.py stamps `sends` as [[unix_ts, value], ...], newest first, on every
    delivered alert. Pick the most recent send at or before the reply; falling back
    to the live value only when there is no send history (a pre-`sends` finding)."""
    try:
        cutoff = int(at.timestamp())
    except Exception:
        cutoff = None
    for row in (f.get("sends") or []):
        try:
            ts, val = int(row[0]), row[1]
        except (TypeError, ValueError, IndexError):
            continue
        if cutoff is None or ts <= cutoff:
            return val
    return f.get("value")


def set_status(s, ident, status, uid, note="", when=None):
    k, f = find(s, ident)
    if not f: return None, None
    at = _sent_at(when)
    f["status"] = status
    f["status_ts"] = at.isoformat()
    f["status_by"] = uid
    f["status_note"] = note[:300]
    f["value_at_status"] = _value_as_of(f, at)
    f["pending_send"] = False
    if status in ("reported", "contained", "closed", "watching"):
        f["ack_by"] = f.get("ack_by") or uid          # stops the routine repeat
        f["ack_ts"] = f.get("ack_ts") or at.isoformat()
    return k, f


CASES_MAX = 25          # a bounded list beats a truncated one


def _plain(f):
    """The case description a reader can act on — the same sentence the alert used.

    `detail` is the detector's own string: `top1=62% n=113`, `340/24h >= 3 x`. That is
    the formula format explain.py exists to remove, and /cases is where every dead end
    in the channel points people."""
    try:
        from explain import measured
    except ImportError:
        try:
            from notify.explain import measured
        except ImportError:
            measured = None
    if measured:
        try:
            return measured(f)
        except Exception as ex:
            print(f"commands: measured() failed for {f.get('id')}: {ex!r}", file=sys.stderr)
    return f.get("signal") or "an alert with no description"


def cases_text(s):
    now = dt.datetime.now(dt.UTC)
    cases = [f for f in (s.get("open") or {}).values()
             if f.get("status") in ("reported", "contained", "watching")
             or (f.get("tier") in ("page", "notify") and not (f.get("ack_by") or f.get("ack_role")))]
    if not cases:
        return "✅ <b>No open cases.</b>\nNothing is waiting on a person right now."
    ordered = sorted(cases, key=lambda x: str(x.get("status_ts") or x.get("first_ts") or ""))
    shown, extra = ordered[:CASES_MAX], len(ordered) - CASES_MAX
    L = [f"<b>📁 Open cases — {len(ordered)}</b>", ""]
    for f in shown:
        st = f.get("status") or "new"
        icon, meaning = STATUSES.get(st, ("🆕", "not yet looked at"))
        age = ""
        try:
            ts = dt.datetime.fromisoformat(str(f.get("status_ts") or "").replace("Z", "+00:00"))
            hrs = (now - ts).total_seconds() / 3600
            age = f" · {hrs:.0f} h ago" if hrs < 48 else f" · {hrs/24:.0f} d ago"
        except Exception:
            pass
        L.append(f"{icon} <b>{st}</b>{age} — {_plain(f)}")
        if f.get("key", "").startswith("0x"):
            L.append(f"    <code>{f['key'][:20]}…</code>  <code>/close {f.get('id')}</code>")
        if f.get("status_note"):
            L.append(f"    \"{_esc(f['status_note'])}\"")
    if extra > 0:
        L += ["", f"<i>{extra} more, oldest shown first. Act on these and they drop off the list.</i>"]
    L += ["", "<i>/reported &lt;id&gt; · /contained &lt;id&gt; · /close &lt;id&gt; · /watching &lt;id&gt;</i>"]
    return "\n".join(L)


def status_text(s):
    """Written for the person who just joined the channel, not for the detector.

    /help calls this "is anything waiting on me right now", so it answers that
    question. Block lag, row counts, mindset age and "acked/seeded" told a new
    reader nothing and were the first screen they ever saw."""
    op = [f for f in (s.get("open") or {}).values() if f.get("ack_by") != "go-live-seed"]
    live = [f for f in op if not f.get("ack_by")]
    pages = [f for f in live if f.get("tier") == "page"]
    held = [f for f in live if f.get("pending_send")]
    hb = {}
    try: hb = json.loads((ROOT / "heartbeat.json").read_text())
    except Exception: pass

    L = ["📋 <b>Where things stand</b>", ""]
    if not live:
        L.append("Nothing is waiting on a person right now.")
    else:
        L.append(f"<b>{len(live)} case(s)</b> nobody has answered yet"
                 + (f", <b>{len(pages)}</b> of them marked <i>needs attention now</i>." if pages else "."))
    inc = s.get("incident")
    if inc:
        L += ["", f"⚠️ <b>Incident mode is on</b> since {str(inc.get('started'))[11:16]} UTC. "
                  f"Individual alerts are being sent silently under one header per run"
                  + (f"; {len(held)} are queued to send in later runs." if held else ".")]
    elif held:
        L += ["", f"{len(held)} alert(s) are queued and will send in the next run(s)."]

    run = str(hb.get("run_ts") or "")
    if run:
        try:
            gap = (dt.datetime.now(dt.UTC)
                   - dt.datetime.fromisoformat(run.replace("Z", "+00:00"))).total_seconds() / 60
            # MEASURED, not intended. The schedule asks for every 10 minutes; GitHub
            # delivers a median of 32 and has taken 102. The figure this used to quote
            # — "p50 13 / p95 56" — came from alerts/weekly.json with samples: 7, where
            # "p95" is gaps[int(7*0.95)], i.e. the largest of seven. Promising ten
            # minutes to somebody deciding whether to wait or escalate is rule 8.
            L += ["", f"<i>I last looked at the chain {gap:.0f} min ago. In practice I manage "
                      f"about every half hour, and gaps of an hour and a half have happened. "
                      f"Silence is not an all-clear.</i>"]
        except Exception:
            L += ["", f"<i>Last checked {run}.</i>"]
    # What I might be MISSING. Without this, /status reads as an all-clear while the
    # detector is blind: "I last looked at the chain 8 min ago" is true and reassuring
    # even when the address set is two days old and every account created since then
    # looks like a stranger. Only a person gives an all-clear (§6.3). The thresholds
    # come from watchdog.py, which already announces both of these to this same group,
    # so this repeats what the channel has been told rather than disclosing anything
    # new — which is why it lives here and not behind a /debug command (§7 is Po's call).
    blind = []
    try:
        age = float(hb.get("mindset_age_h") or 0)
        if age > watchdog.MINDSET_STALE_H:
            blind.append(f"my list of platform addresses is {age:.0f} h old, so accounts made "
                         f"since then look like strangers to me")
    except (TypeError, ValueError):
        pass
    try:
        behind = float(hb.get("lag_blocks") or 0)
        if behind > watchdog.LAG_BLOCKS_MAX:
            # Base blocks are a fixed 2 s, so blocks convert straight to minutes.
            blind.append(f"I am about {behind * 2 / 60:.0f} min behind the chain, so the newest "
                         f"transfers are not in anything above")
    except (TypeError, ValueError):
        pass
    if blind:
        L += ["", "⚠️ <b>I am working degraded right now</b>"] + [f"• {b}" for b in blind]

    for f in sorted(live, key=lambda x: 0 if x.get("tier") == "page" else 1)[:5]:
        L.append(f"• {_plain(f)}  <code>{f.get('id')}</code>")
    if len(live) > 5:
        L.append(f"<i>…and {len(live) - 5} more — send /cases for the full list.</i>")
    return "\n".join(L)


def main():
    s = load(); off = int(s.get("telegram_offset") or 0)
    r = api("getUpdates", offset=off + 1 if off else 0, timeout=0, allowed_updates=json.dumps(["message"]))
    if not r.get("ok"):
        # A revoked token, a Telegram outage and a genuinely quiet channel used to
        # print the same line and exit 0. This is the only control surface in the
        # system; if it is dead the run must go red. crawl.yml still runs the sender
        # afterwards, so alerts keep flowing while this is broken.
        print(f"commands: getUpdates FAILED ({r.get('error')}) — replies are NOT being read")
        return 2
    ups = r.get("result") or []
    if not ups:
        print("commands: no updates"); return 0
    changed = 0
    for u in ups:
        # The offset advances even if this update fails, and state is saved either way.
        # Otherwise one malformed message replays the same crash every run and the
        # whole command surface dies silently.
        s["telegram_offset"] = max(int(s.get("telegram_offset") or 0), int(u.get("update_id", 0)))
        try:
            changed += handle(s, u.get("message") or {})
        except Exception as e:
            OUTCOME.update(outcome="error", action=type(e).__name__)
            print(f"commands: update skipped ({type(e).__name__})")
            try:
                reply("\u26a0\ufe0f I could not process that message \u2014 <b>your case state was NOT changed</b>. "
                      "Send <code>/cases</code> to see where things stand.", (u.get("message") or {}).get("message_id"))
            except Exception:
                pass
        finally:
            _log_in(u)
    save(s)
    print(f"commands: {len(ups)} update(s), {changed} state change(s)")
    if UNDELIVERED:
        # The state change (if any) is saved above on purpose — the decision the
        # person made is real even when the confirmation of it did not land. What
        # must NOT happen is the run reporting green while somebody is waiting for
        # an answer that Telegram refused.
        for u in UNDELIVERED:
            print(f"commands: undelivered reply to message {u['reply_to']}: {u['starts']!r}",
                  file=sys.stderr)
        print(f"commands: {len(UNDELIVERED)} reply/replies NOT delivered — the run is RED")
        return 3
    return 0


# ---------------------------------------------------------------- the plain-word reply
# The footer on every alert tells a non-technical reader to answer in ordinary words,
# so this is the primary control surface. It must never act on a sentence that only
# happens to contain one of these words: "I closed the support ticket" closed a live
# security case, and "not yet contained" recorded CONTAINED, which flips the case to
# page on any further activity. When the message is not plainly one thing, record
# nothing and ask.
_WORD_STATUS = {
    "contained": "contained", "fixed": "contained",
    "reported": "reported", "escalated": "reported",
    "closed": "closed", "close": "closed", "resolved": "closed",
    "watching": "watching", "noted": "watching",
}
_NEG = {"not", "no", "nothing", "none", "never", "cannot", "yet", "unresolved", "uncontained",
        "isn't", "isnt", "aren't", "arent", "wasn't", "wasnt", "haven't", "havent",
        "hasn't", "hasnt", "don't", "dont", "didn't", "didnt", "won't", "wont",
        "can't", "cant", "couldn't", "couldnt", "still"}


def read_status(text):
    """(status, guess). `status` is set ONLY when the message says one thing plainly.

    Accepted: the message starts with the word ("contained", "contained — wallet
    restricted"), or it is three words or fewer and contains exactly one of them
    ("all closed now"). Anything with a negation, a hedge, or two different status
    words returns no status and a guess to confirm."""
    tokens = re.findall(r"[a-z']+", str(text or "").lower())
    if not tokens:
        return None, None
    found = [_WORD_STATUS[w] for w in tokens if w in _WORD_STATUS]
    if not found:
        return None, None
    guess = found[0]
    if any(w in _NEG for w in tokens) or len(set(found)) > 1:
        return None, guess
    first = _WORD_STATUS.get(tokens[0])
    if first:
        return first, first
    if len(tokens) <= 3:
        return guess, guess
    return None, guess


def handle(s, m):
    """One message. Returns 1 if it changed case state."""
    OUTCOME.clear(); OUTCOME["outcome"] = "seen"
    text = (m.get("text") or "").strip()
    uid  = str((m.get("from") or {}).get("id", ""))
    mid  = m.get("message_id")
    # WHICH CHAT. reply() can only post to CHAT(), so without this any stranger who
    # finds the bot could make it print into the security group from a DM \u2014 and
    # Telegram message ids are per-chat, so a DM reply id can collide with a group
    # alert id and resolve to somebody else's case.
    chat = str((m.get("chat") or {}).get("id", ""))
    if CHAT() and chat and chat != str(CHAT()):
        OUTCOME.update(outcome="ignored: another chat", redact=True)
        print(f"commands: ignoring a message from another chat (id withheld)")
        return 0
    # fail CLOSED: with no authorised list configured nobody may change state
    allowed = bool(ACK()) and uid in ACK()

    def deny():
        OUTCOME.update(outcome="denied", action="not on the on-call list")
        return _deny(uid, mid)

    rk, rf = find_by_reply(s, m)
    if rf:
        OUTCOME["case"] = rf.get("id")

    if not text.startswith("/"):
        if not rf:
            # A person wrote something and expects to be heard. Silence here is how a
            # reply at 01:32 disappeared with no trace. Always answer.
            rt = (m.get("reply_to_message") or {}).get("message_id")
            if rt:
                # The ledger usually knows WHAT that message was even when it is not a
                # case, so say it. Only when it does not is the honest answer the old
                # one: do not diagnose, and never claim the message was not recorded.
                what = None
                try:
                    what = msglog.describe_words(rt)
                except Exception as e:
                    print(f"commands: msglog.describe failed ({type(e).__name__})", file=sys.stderr)
                OUTCOME.update(outcome="unmatched", action="asked for the id")
                if what:
                    reply(f"I saw your reply and I have <b>not</b> changed anything: you replied "
                          f"to <b>{what}</b>, which is not a case on its own.\n\n"
                          f"Reply to the alert itself, or send <code>/cases</code> and use "
                          f"<code>/contained &lt;id&gt;</code>. If that message really was about a "
                          f"case, reply to it with <code>/link &lt;id&gt;</code> and I will "
                          f"remember the connection.", mid)
                else:
                    reply("I saw your reply and I have <b>not</b> changed anything, because I "
                          "cannot tell which case that message was about. It may not be a case at "
                          "all (a digest, a health notice, or one of my own replies), or it may be "
                          "old enough that I no longer hold the link.\n\n"
                          "Send <code>/cases</code> and use <code>/contained &lt;id&gt;</code>, or "
                          "reply to a recent alert.", mid)
            elif any(w in text.lower() for w in ("contained", "reported", "closed", "watching", "fixed", "paused", "done")):
                OUTCOME.update(outcome="unmatched", action="no reply target")
                reply("I think you are telling me about a case, but I do not know which one. "
                      "Reply directly to the alert, or send <code>/cases</code> to see the ids.", mid)
            return 0
        st, guess = read_status(text)
        if not st:
            OUTCOME.update(outcome="matched", action=f"asked to confirm '{guess}'" if guess else "asked for a word")
            if guess:
                # Never guess on the reader's behalf. "I closed the support ticket"
                # closed a live security case; "not yet contained" recorded CONTAINED,
                # which flips the case to page-on-any-activity.
                reply(f"I am not sure what you meant, so <b>nothing was changed</b>. "
                      f"Did you mean <b>{guess}</b>? Reply with just that one word to record it.\n"
                      f"<i>Your words: \u201c{_esc(text[:120])}\u201d</i>", mid)
            else:
                reply("Got it \u2014 to record it against this case, reply with one of: "
                      "<b>reported</b> \u00b7 <b>contained</b> \u00b7 <b>watching</b> \u00b7 <b>closed</b>", mid)
            return 0
        if not allowed:
            deny(); return 0
        set_status(s, rf.get("id"), st, uid, text[:300], when=m.get("date"))
        OUTCOME.update(outcome="matched", action=st)
        icon, meaning = STATUSES.get(st, ("\U0001f440", ""))
        extra = {"reported": "I will stay quiet unless it keeps growing.",
                 "contained": "Any further activity will page you \u2014 that would mean the fix did not hold.",
                 "closed": "Removed from open cases; still in the record.",
                 "watching": "No repeats; I will tell you if it changes materially."}.get(st, "")
        when = m.get("date")
        seen = ""
        if when:
            gap = (dt.datetime.now(dt.UTC) - dt.datetime.fromtimestamp(int(when), dt.UTC)).total_seconds() / 60
            if gap > 3: seen = f"\n<i>You sent this {gap:.0f} min ago; I read replies when I run, which is about every half hour.</i>"
        reply(f"{icon} recorded as <b>{st}</b> ({meaning}) for <code>{rf.get('id')}</code>"
              + (f" \u00b7 <code>{str(rf.get('key'))[:12]}\u2026</code>" if str(rf.get("key", "")).startswith("0x") else "")
              + f"\n{extra}{seen}", mid)
        return 1

    cmd, *args = text.split()
    cmd = cmd.split("@")[0].lower()
    OUTCOME.update(outcome="command", action=cmd)
    CASE_CMDS = ("/reported", "/contained", "/close", "/watching", "/reopen", "/ack",
                 "/snooze", "/quiet")
    # Verbs the reader was TOLD to use, which still fall through to the catch-all when
    # the case id is missing. Those must be answered with "that needs a case id", never
    # with "I do not know that": /help names /quiet and /snooze, and a person told the
    # quieting command does not exist stops trying to quiet the case.
    NEEDS_ID = CASE_CMDS + ("/unmute", "/link")
    if rf and cmd in CASE_CMDS and (not args or not find(s, args[0])[1]):
        args = [rf.get("id")] + list(args)          # replying supplies the case id

    if cmd == "/status":
        reply(status_text(s), mid)
    elif cmd == "/cases":
        reply(cases_text(s), mid)
    elif cmd == "/help":
        reply("<b>Reading the situation</b>\n"
              "/status \u2014 is anything waiting on me right now\n"
              "/cases \u2014 everything open, and how long it has been open\n\n"
              "<b>Telling me where a case stands</b> (or just reply to the alert with the word)\n"
              "/reported &lt;id&gt; [note] \u2014 team informed. I go quiet unless it keeps growing.\n"
              "/contained &lt;id&gt; [what was done] \u2014 a fix is in. Any further activity pages you.\n"
              "/watching &lt;id&gt; \u00b7 /close &lt;id&gt; \u00b7 /reopen &lt;id&gt;\n\n"
              "<b>Quieting one case</b>\n"
              "/snooze &lt;id&gt; &lt;hours&gt; \u2014 quiet for that long, then it arrives anyway. "
              "<code>/quiet</code> is the same command.\n"
              "Nothing quiet is thrown away, and there is no way to silence a whole signal.\n\n"
              "<b>When I cannot tell which case you mean</b>\n"
              "Reply to the message and send /link &lt;id&gt; \u2014 I will remember that "
              "message, and anything replying to it, as that case.", mid)
    elif cmd in ("/reported", "/contained", "/close", "/watching", "/reopen") and args:
        if not allowed:
            deny(); return 0
        st = {"/reported": "reported", "/contained": "contained", "/close": "closed",
              "/watching": "watching", "/reopen": None}[cmd]
        if cmd == "/reopen":
            k, f = find(s, args[0])
            if not f:
                reply(f"no case matching <code>{_esc(args[0])}</code>", mid); return 0
            for fld in ("status", "status_ts", "status_note", "ack_by", "ack_ts", "value_at_status"):
                f.pop(fld, None)
            reply(f"\u21a9\ufe0f reopened <code>{f.get('id')}</code> \u2014 it will alert again if it fires", mid)
        else:
            k, f = set_status(s, args[0], st, uid, " ".join(args[1:]), when=m.get("date"))
            if not f:
                reply(f"no case matching <code>{_esc(args[0])}</code>", mid); return 0
            icon, meaning = STATUSES[st]
            extra = {"reported": "I will stay quiet unless it keeps growing.",
                     "contained": "Any further activity from now on pages you \u2014 that would mean the fix did not hold.",
                     "closed": "Removed from open cases; still in the record.",
                     "watching": "No repeats; I will tell you if it changes materially."}[st]
            reply(f"{icon} <b>{st}</b> \u2014 <code>{f.get('id')}</code> ({meaning})\n{extra}", mid)
        return 1
    elif cmd == "/ack":
        # THE RETIRED VERB (council \u00a75, vote 9). /ack wrote ack_by and nothing else, so
        # _suppressed() returned "acked" for ever: the case left /cases, never repeated,
        # and could not escalate \u2014 a mute button under an acknowledging name, printed as
        # the loudest instruction on every alert. The FIELD stays (state_sync.HUMAN_FIELDS,
        # prune()._settled and _suppressed all read it); the VERB is what is being removed.
        # Deleting this branch would drop the muscle-memory typist into `return 0`, which
        # is silence (\u00a76.2), and quietly re-routing them to `watching` would change what
        # they asked for behind their back. So it does the nearest honest thing \u2014
        # `watching` keeps the case in /cases and lets run.py escalate it if it grows
        # 1.5x \u2014 and the confirmation says the verb is gone and what was recorded.
        if not allowed:
            deny(); return 0
        if not args:
            reply("<code>/ack</code> is retired \u2014 it silenced a case for good instead of "
                  "recording a decision, including the alert that would have told you a fix "
                  "did not hold. <b>Nothing was changed.</b>\n"
                  "Reply to the alert with <b>watching</b>, or send <code>/cases</code> for the "
                  "ids and use <code>/watching &lt;id&gt;</code>. To quiet one case for a while: "
                  "<code>/snooze &lt;id&gt; &lt;hours&gt;</code>.", mid)
            return 0
        k, f = set_status(s, args[0], "watching", uid, " ".join(args[1:]), when=m.get("date"))
        if not f:
            reply(f"no case matching <code>{_esc(args[0])}</code> \u2014 send "
                  f"<code>/cases</code> for the ids", mid)
            return 0
        OUTCOME.update(case=f.get("id"), action="watching (/ack is retired)")
        reply(f"\U0001f440 <b>watching</b> \u2014 <code>{f.get('id')}</code> (seen, still "
              f"watching)\nNo repeats; I will tell you if it changes materially.\n"
              f"<i><code>/ack</code> is retired: it used to silence the case permanently. I "
              f"recorded <b>watching</b> instead, which keeps it in <code>/cases</code>. If you "
              f"meant \u201cquiet for a while\u201d, send <code>/snooze {f.get('id')} 6</code>.</i>",
              mid)
        return 1
    elif cmd in ("/snooze", "/quiet") and args:
        # `/quiet <id>` is the council's name for this (\u00a75, vote 8) and `/snooze` is the
        # name every message already in the channel uses, so both spellings run the same
        # branch. What the council cut was the SCOPE of `/mute <signal>`: this is ONE
        # case, and telegram._suppressed()/send_pending() HOLD it (pending_send stays
        # True) instead of clearing it, so it arrives when the timer ends (\u00a76.6).
        if not allowed:
            deny(); return 0
        k, f = find(s, args[0])
        if not f:
            reply(f"no open finding matching <code>{_esc(args[0])}</code>", mid); return 0
        # Timed from when the person typed it, not from when the poll read it: "quiet
        # for 6 h" typed at 02:20 and read at 03:16 bought 56 minutes of silence nobody
        # asked for (the same defect as status_ts, fix-round critic #5).
        base = _sent_at(m.get("date"))
        h = float(args[1]) if len(args) > 1 and args[1].replace(".", "").isdigit() else 6
        until = base + dt.timedelta(hours=h)
        f["snooze_until"] = until.isoformat()
        if until <= dt.datetime.now(dt.UTC):
            # Never tell a reader a case is quiet when it is not: the window they asked
            # for closed while their message sat in the poll queue.
            reply(f"\U0001f634 <code>{f.get('id')}</code> \u2014 the {h:g} h you asked for ran "
                  f"from when you sent this ({base:%H:%M} UTC) and has already passed, so this "
                  f"case is <b>not</b> quiet: it sends in the next run. Send it again for "
                  f"{h:g} h from now.", mid)
        else:
            reply(f"\U0001f634 quiet \u2014 <code>{f.get('id')}</code> until <b>{until:%H:%M} "
                  f"UTC</b> ({h:g} h from when you sent this).\nIt is <b>held, not dropped</b>: "
                  f"it arrives then. Mark it <b>contained</b> instead if you want any further "
                  f"activity to page you straight away.", mid)
        return 1
    elif cmd == "/link":
        # The permanent limit this cannot fix: Telegram will not enumerate messages
        # the bot sent before the ledger existed, and it cannot be asked what an
        # arbitrary id was. `/link` is the manual route for exactly those.
        if not allowed:
            deny(); return 0
        target = (m.get("reply_to_message") or {}).get("message_id")
        if not target or not args:
            reply("Reply to the message you want me to connect, and send "
                  "<code>/link &lt;case id&gt;</code>. I will remember that message — and "
                  "anything replying to it — as belonging to that case.", mid)
            return 0
        k, f = find(s, args[0])
        if not f:
            reply(f"no case matching <code>{_esc(args[0])}</code> — send <code>/cases</code> "
                  f"for the ids", mid)
            return 0
        try:
            msglog.link(target, f.get("id"))
        except Exception as e:
            print(f"commands: msglog.link failed ({type(e).__name__})", file=sys.stderr)
            reply("I could not write that link down, so I have <b>not</b> promised it. "
                  "Use <code>/contained &lt;id&gt;</code> against the case directly.", mid)
            return 0
        s.setdefault("by_message", {})[str(target)] = f.get("id")
        OUTCOME.update(case=f.get("id"), action="/link")
        reply(f"\U0001f517 linked that message to <code>{f.get('id')}</code>. Replying to it "
              f"will now record against that case.", mid)
        return 1
    elif cmd == "/unmute" and args:
        if not allowed:
            deny(); return 0
        sig = args[0]
        gone = (s.get("muted") or {}).pop(sig, None)
        reply(f"\U0001f50a <b>{_esc(sig)}</b> is not muted" + (" any more." if gone else " \u2014 nothing to clear."), mid)
        return 1 if gone else 0
    elif cmd == "/mute":
        reply("\u26a0\ufe0f <code>/mute</code> is gone. It muted a whole signal for everyone at "
              "once and quietly threw away every alert that arrived while it was on.\n"
              "Use <code>/snooze &lt;id&gt; &lt;hours&gt;</code>, also spelled <code>/quiet</code> "
              "\u2014 it is one case, and the alert still arrives when the time is up.", mid)
        return 0
    elif "@" not in text.split()[0]:
        # Every branch above answers. Without this one, a command that reached none of
        # them \u2014 a typo, a retired verb, /contained with no id and no reply target \u2014
        # fell off the end into `return 0`, which is silence, and silence is the failure
        # this whole surface exists to remove (\u00a76.2). A command explicitly addressed to
        # another bot (/foo@SomeOtherBot) is left alone: answering those would make this
        # bot talk over every other bot in the group.
        if cmd in NEEDS_ID:
            OUTCOME.update(outcome="command", action=f"{cmd} without a usable id")
            reply(f"<code>{_esc(cmd)}</code> needs a case id and I could not find one \u2014 "
                  f"<b>nothing was changed</b>.\n"
                  f"Reply to the alert and send it again, or send <code>/cases</code> for "
                  f"the ids.", mid)
            return 0
        OUTCOME.update(outcome="command", action=f"{cmd} not understood")
        reply(f"I do not know <code>{_esc(cmd)}</code> \u2014 <b>nothing was changed</b>.\n"
              f"<code>/cases</code> \u00b7 <code>/status</code> \u00b7 <code>/help</code>, or "
              f"reply to an alert with <b>reported</b> \u00b7 <b>contained</b> \u00b7 "
              f"<b>watching</b> \u00b7 <b>closed</b>.", mid)
        return 0
    return 0


if __name__ == "__main__": sys.exit(main())
