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

def reply(text, to=None):
    p = {"chat_id": CHAT(), "text": text[:4000], "parse_mode": "HTML", "disable_notification": "true"}
    if to: p["reply_to_message_id"] = to
    return api("sendMessage", **p)

def _owner_name():
    try:
        return json.loads((ROOT / "detect" / "thresholds.json").read_text()).get("escalation_owner", "the on-call")
    except Exception:
        return "the on-call"


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


def find(s, ident):
    ident = (ident or "").strip().lower()
    for k, f in (s.get("open") or {}).items():
        if str(f.get("id", "")).lower() == ident or k.lower() == ident: return k, f
    for k, f in (s.get("open") or {}).items():           # allow entity-prefix match
        if ident and str(f.get("key", "")).lower().startswith(ident): return k, f
    return None, None

STATUSES = {
    "reported":  ("📣", "team informed, action pending"),
    "contained": ("🛡", "a fix was applied"),
    "closed":    ("✅", "resolved"),
    "watching":  ("👀", "seen, still watching"),
}


def set_status(s, ident, status, uid, note=""):
    k, f = find(s, ident)
    if not f: return None, None
    now = dt.datetime.now(dt.UTC)
    f["status"] = status
    f["status_ts"] = now.isoformat()
    f["status_by"] = uid
    f["status_note"] = note[:300]
    f["value_at_status"] = f.get("value")
    f["pending_send"] = False
    if status in ("reported", "contained", "closed", "watching"):
        f["ack_by"] = f.get("ack_by") or uid          # stops the routine repeat
        f["ack_ts"] = f.get("ack_ts") or now.isoformat()
    return k, f


def cases_text(s):
    now = dt.datetime.now(dt.UTC)
    cases = [f for f in (s.get("open") or {}).values()
             if f.get("status") in ("reported", "contained", "watching")
             or (f.get("tier") in ("page", "notify") and not (f.get("ack_by") or f.get("ack_role")))]
    if not cases:
        return "✅ <b>No open cases.</b>\nNothing is waiting on a person right now."
    L = ["<b>📁 Open cases</b>", ""]
    for f in sorted(cases, key=lambda x: str(x.get("status_ts") or x.get("first_ts") or "")):
        st = f.get("status") or "new"
        icon, meaning = STATUSES.get(st, ("🆕", "not yet looked at"))
        age = ""
        try:
            ts = dt.datetime.fromisoformat(str(f.get("status_ts") or "").replace("Z", "+00:00"))
            hrs = (now - ts).total_seconds() / 3600
            age = f" · {hrs:.0f} h ago" if hrs < 48 else f" · {hrs/24:.0f} d ago"
        except Exception:
            pass
        L.append(f"{icon} <b>{st}</b>{age} — {f.get('detail') or f.get('signal')}")
        if f.get("key", "").startswith("0x"):
            L.append(f"    <code>{f['key'][:20]}…</code>  <code>/close {f.get('id')}</code>")
        if f.get("status_note"):
            L.append(f"    \"{f['status_note']}\"")
    L += ["", "<i>/reported &lt;id&gt; · /contained &lt;id&gt; · /close &lt;id&gt; · /watching &lt;id&gt;</i>"]
    return "\n".join(L)


def status_text(s):
    op = [f for f in (s.get("open") or {}).values() if f.get("ack_by") != "go-live-seed"]
    live = [f for f in op if not f.get("ack_by")]
    tiers = {}
    for f in live: tiers[f.get("tier")] = tiers.get(f.get("tier"), 0) + 1
    hb = {}
    try: hb = json.loads((ROOT / "heartbeat.json").read_text())
    except Exception: pass
    L = ["📋 <b>status</b>",
         f"open unacked: {len(live)}" + (f"  ({', '.join(f'{k}:{v}' for k, v in sorted(tiers.items()))})" if tiers else ""),
         f"acked/seeded: {len(s.get('open') or {}) - len(live)}",
         f"last run: {hb.get('run_ts','?')} · lag {hb.get('lag_blocks','?')} blocks · rows {hb.get('rows_total','?')}",
         f"mindset: {hb.get('mindset_source','?')} ({hb.get('mindset_age_h','?')} h old)"]
    for f in sorted(live, key=lambda x: 0 if x.get("tier") == "page" else 1)[:8]:
        L.append(f"• {f.get('tier')} {f.get('signal')} <code>{str(f.get('key'))[:12]}</code> {f.get('detail','')} <code>{f.get('id')}</code>")
    return "\n".join(L)

def main():
    s = load(); off = int(s.get("telegram_offset") or 0)
    r = api("getUpdates", offset=off + 1 if off else 0, timeout=0, allowed_updates=json.dumps(["message"]))
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
    return 0


def handle(s, m):
    """One message. Returns 1 if it changed case state."""
    text = (m.get("text") or "").strip()
    uid  = str((m.get("from") or {}).get("id", ""))
    mid  = m.get("message_id")
    # fail CLOSED: with no authorised list configured nobody may change state
    allowed = bool(ACK()) and uid in ACK()
    deny = lambda: reply(f"Only the on-call can change a case. Ask {_owner_name()} to be added.", mid)
    rk, rf = find_by_reply(s, m)

    if not text.startswith("/"):
        if not rf:
            return 0
        low = text.lower()
        # Whole words, in a fixed order. A substring scan is dangerous here:
        # "attack" contains "ack", so "this looks like an attack" would have
        # silenced the very case it was reporting.
        WORDS = [("contained", "contained"), ("fixed", "contained"),
                 ("reported", "reported"), ("escalated", "reported"),
                 ("closed", "closed"), ("resolved", "closed"),
                 ("watching", "watching"), ("noted", "watching")]
        st = None
        for w, mapped in WORDS:
            if re.search(rf"\b{w}\b", low):
                if re.search(rf"\bnot\s+{w}\b", low):
                    st = None
                    break
                st = mapped
                break
        if not st:
            reply("Got it \u2014 to record it against this case, reply with one of: "
                  "<b>reported</b> \u00b7 <b>contained</b> \u00b7 <b>watching</b> \u00b7 <b>closed</b>", mid)
            return 0
        if not allowed:
            deny(); return 0
        set_status(s, rf.get("id"), st, uid, text[:300])
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
        reply(f"{icon} recorded as <b>{st}</b> for this case ({meaning})\n{extra}{seen}", mid)
        return 1

    cmd, *args = text.split()
    cmd = cmd.split("@")[0].lower()
    if rf and (not args or not find(s, args[0])[1]):
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
              "/ack &lt;id&gt; [note] \u00b7 /snooze &lt;id&gt; &lt;hours&gt; \u00b7 /mute &lt;signal&gt; &lt;hours&gt; \u00b7 /unmute &lt;signal&gt;", mid)
    elif cmd in ("/reported", "/contained", "/close", "/watching", "/reopen") and args:
        if not allowed:
            deny(); return 0
        st = {"/reported": "reported", "/contained": "contained", "/close": "closed",
              "/watching": "watching", "/reopen": None}[cmd]
        if cmd == "/reopen":
            k, f = find(s, args[0])
            if not f:
                reply(f"no case matching <code>{args[0]}</code>", mid); return 0
            for fld in ("status", "status_ts", "status_note", "ack_by", "ack_ts", "value_at_status"):
                f.pop(fld, None)
            reply(f"\u21a9\ufe0f reopened <code>{f.get('id')}</code> \u2014 it will alert again if it fires", mid)
        else:
            k, f = set_status(s, args[0], st, uid, " ".join(args[1:]))
            if not f:
                reply(f"no case matching <code>{args[0]}</code>", mid); return 0
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
            reply(f"no open finding matching <code>{args[0]}</code>", mid); return 0
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
    elif cmd in ("/mute", "/unmute") and args:
        if not allowed:
            deny(); return 0
        sig = args[0]; mutes = s.setdefault("muted", {})
        if cmd == "/mute":
            h = float(args[1]) if len(args) > 1 and args[1].replace(".", "").isdigit() else 6
            mutes[sig] = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=h)).isoformat()
            reply(f"\U0001f507 muted <b>{sig}</b> for {h:g} h", mid)
        else:
            mutes.pop(sig, None); reply(f"\U0001f50a unmuted <b>{sig}</b>", mid)
        return 1
    return 0


if __name__ == "__main__": sys.exit(main())
