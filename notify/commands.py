#!/usr/bin/env python3
"""Telegram command handler — runs once per slot inside the workflow.

Reads new updates with getUpdates (offset persisted in alerts/state.json), applies
/ack /snooze /mute /status /help, and replies in-thread. Privacy mode may stay ON:
commands are always delivered to bots.

Only user ids listed in TELEGRAM_ACK_USER_IDS may change state.
"""
import json, os, pathlib, re, sys, urllib.parse, urllib.request, datetime as dt

ROOT  = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "alerts" / "state.json"
HOME  = pathlib.Path.home() / ".moca-ledger"

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


def load(): return json.loads(STATE.read_text()) if STATE.exists() else {"open": {}, "telegram_offset": 0}
def save(s): STATE.write_text(json.dumps(s, indent=1))

def find_by_reply(s, m):
    """Which case is this message a reply to? Telegram delivers replies to the bot
    even with privacy mode on, so replying to an alert is the natural way to act on it."""
    r = m.get("reply_to_message") or {}
    mid = r.get("message_id")
    if not mid: return None, None
    fid = (s.get("by_message") or {}).get(str(mid))
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
            L += ["", f"<i>I last looked at the chain {gap:.0f} min ago. I check about every 10 "
                      f"minutes, sometimes up to an hour. Silence is not an all-clear.</i>"]
        except Exception:
            L += ["", f"<i>Last checked {run}.</i>"]
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
            print(f"commands: update skipped ({type(e).__name__})")
            try:
                reply("\u26a0\ufe0f I could not process that message \u2014 <b>your case state was NOT changed</b>. "
                      "Send <code>/cases</code> to see where things stand.", (u.get("message") or {}).get("message_id"))
            except Exception:
                pass
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
    text = (m.get("text") or "").strip()
    uid  = str((m.get("from") or {}).get("id", ""))
    mid  = m.get("message_id")
    # WHICH CHAT. reply() can only post to CHAT(), so without this any stranger who
    # finds the bot could make it print into the security group from a DM \u2014 and
    # Telegram message ids are per-chat, so a DM reply id can collide with a group
    # alert id and resolve to somebody else's case.
    chat = str((m.get("chat") or {}).get("id", ""))
    if CHAT() and chat and chat != str(CHAT()):
        print(f"commands: ignoring a message from another chat (id withheld)")
        return 0
    # fail CLOSED: with no authorised list configured nobody may change state
    allowed = bool(ACK()) and uid in ACK()
    deny = lambda: _deny(uid, mid)
    rk, rf = find_by_reply(s, m)

    if not text.startswith("/"):
        if not rf:
            # A person wrote something and expects to be heard. Silence here is how a
            # reply at 01:32 disappeared with no trace. Always answer.
            if (m.get("reply_to_message") or {}).get("message_id"):
                # Do not diagnose. The message may be the digest, a health notice, one of
                # my own confirmations, or an alert old enough that its id has aged out of
                # the reply map \u2014 I cannot tell which, and claiming it was never recorded
                # is usually false.
                reply("I saw your reply and I have <b>not</b> changed anything, because I "
                      "cannot tell which case that message was about. It may not be a case at "
                      "all (a digest, a health notice, or one of my own replies), or it may be "
                      "old enough that I no longer hold the link.\n\n"
                      "Send <code>/cases</code> and use <code>/contained &lt;id&gt;</code>, or "
                      "reply to a recent alert.", mid)
            elif any(w in text.lower() for w in ("contained", "reported", "closed", "watching", "fixed", "paused", "done")):
                reply("I think you are telling me about a case, but I do not know which one. "
                      "Reply directly to the alert, or send <code>/cases</code> to see the ids.", mid)
            return 0
        st, guess = read_status(text)
        if not st:
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
        icon, meaning = STATUSES.get(st, ("\U0001f440", ""))
        extra = {"reported": "I will stay quiet unless it keeps growing.",
                 "contained": "Any further activity will page you \u2014 that would mean the fix did not hold.",
                 "closed": "Removed from open cases; still in the record.",
                 "watching": "No repeats; I will tell you if it changes materially."}.get(st, "")
        when = m.get("date")
        seen = ""
        if when:
            gap = (dt.datetime.now(dt.UTC) - dt.datetime.fromtimestamp(int(when), dt.UTC)).total_seconds() / 60
            if gap > 3: seen = f"\n<i>You sent this {gap:.0f} min ago; I read replies about every 10 minutes.</i>"
        reply(f"{icon} recorded as <b>{st}</b> ({meaning}) for <code>{rf.get('id')}</code>"
              + (f" \u00b7 <code>{str(rf.get('key'))[:12]}\u2026</code>" if str(rf.get("key", "")).startswith("0x") else "")
              + f"\n{extra}{seen}", mid)
        return 1

    cmd, *args = text.split()
    cmd = cmd.split("@")[0].lower()
    CASE_CMDS = ("/reported", "/contained", "/close", "/watching", "/reopen", "/ack", "/snooze")
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
              "<b>Quieting noise</b>\n"
              "/snooze &lt;id&gt; &lt;hours&gt; \u2014 one case, quiet for that long. It still arrives afterwards.\n"
              "/ack &lt;id&gt; [note] \u2014 one case, no more routine repeats.", mid)
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
    elif cmd in ("/ack", "/snooze") and args:
        if not allowed:
            deny(); return 0
        k, f = find(s, args[0])
        if not f:
            reply(f"no open finding matching <code>{_esc(args[0])}</code>", mid); return 0
        now = dt.datetime.now(dt.UTC)
        if cmd == "/ack":
            f["ack_by"] = uid; f["ack_ts"] = now.isoformat()
            if len(args) > 1: f["ack_note"] = " ".join(args[1:])[:200]
            reply(f"\u2705 acked <code>{f.get('id')}</code> \u00b7 {f.get('signal')}", mid)
        else:
            h = float(args[1]) if len(args) > 1 and args[1].replace(".", "").isdigit() else 6
            f["snooze_until"] = (now + dt.timedelta(hours=h)).isoformat()
            reply(f"\U0001f634 snoozed <code>{f.get('id')}</code> for {h:g} h", mid)
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
              "Use <code>/snooze &lt;id&gt; &lt;hours&gt;</code> \u2014 it is one case, and the alert "
              "still arrives when the time is up.", mid)
        return 0
    return 0


if __name__ == "__main__": sys.exit(main())
