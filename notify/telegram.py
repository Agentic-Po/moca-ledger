#!/usr/bin/env python3
"""Telegram sender for the detection floor.

Tiers:  page (loud, photo+text twin)   notify (photo+caption)   digest (silent, batched)
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
            if e.code == 429:
                time.sleep(int(json.loads(e.read()).get("parameters", {}).get("retry_after", 5)))
                continue
            return {"ok": False, "error": f"http {e.code}"}
        except Exception as ex:
            if attempt == 2: return {"ok": False, "error": str(ex)[:80]}
            time.sleep(2)
    return {"ok": False, "error": "retries exhausted"}

CAPTION_MAX = 1024      # Telegram's cap on a photo caption


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
    return r

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
    if f.get("owner"): lines.append(f"owner: {f['owner']}")
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
    """An acked, snoozed or muted finding must never re-send — otherwise every run
    re-pages what the human already handled."""
    if f.get("ack_by") or f.get("ack_role"): return "acked"
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


def _thresholds():
    try:
        return json.loads((ROOT / "detect" / "thresholds.json").read_text())
    except Exception:
        return {}


def _wallet_of(f):
    """Dedupe key: the wallet, or the raw finding key for platform-wide findings."""
    k = str(f.get("key") or "").lower()
    return k[5:] if k.startswith("exit:") else k


def _short(key):
    k = str(key or "")
    if k.lower().startswith("exit:"):
        k = k[5:]
    return (k[:10] + "…") if k.lower().startswith("0x") and len(k) > 12 else (k or "?")


def _is_cashout(f):
    """A first move into an exchange deposit address — the moment a freeze request
    has the best chance of working. balance_watch puts the role in the headline."""
    return str(f.get("signal")) == "S-X" and any(
        "exchange_deposit" in str(h) for h in (f.get("headline") or []))


def _sound_reason(f, inc):
    """Why this finding is allowed to make a sound inside an incident. None = silent."""
    if f.get("escalation") == "activity after containment" or f.get("status") == "contained":
        return "a case marked contained is active again"
    if _is_cashout(f) and not inc.get("cashout"):
        return "first cash-out destination in this incident"
    sig = str(f.get("signal") or "?")
    if sig not in (inc.get("classes") or []):
        return f"first {sig} alert in this incident"
    return None


def _money_totals(findings):
    """(total MOCA, MOCA/h, {wallet: (total, rate)}) deduped by wallet.

    Two signals can fire on the same wallet; adding both totals would report the
    same money twice. Largest total per wallet wins."""
    per = {}
    for f in findings:
        try:
            t = float(f.get("moca_since"))
        except (TypeError, ValueError):
            continue
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


def _incident_state(s, n_loud, now):
    """(inc, on, doubled). Opens, carries or expires the one incident object."""
    thr_min = int(_thresholds().get("incident_mode_min", 3) or 3)
    inc = s.get("incident") or {}
    if inc and now - float(inc.get("last_ts") or 0) > INCIDENT_TTL_S:
        inc = {}
    prev = int(s.get("loud_prev") or 0)
    doubled = prev > 0 and n_loud >= 2 * prev
    on = n_loud > thr_min or (bool(inc) and n_loud > 0)
    if on and not inc:
        inc = {"started": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
               "runs": 0, "classes": [], "cashout": False}
    return inc, on, doubled


def _incident_header(loud, n_shown, new_since, prev_loud, doubled, last_run):
    """The single loud message. Counts, money, top wallets, and the limits of all
    of it — a reader who only ever sees this message must not be misled by it."""
    try:
        from explain import HEDGE
    except ImportError:
        from notify.explain import HEDGE
    total, rate, per = _money_totals(loud)
    L = [f"🚨 <b>Incident mode — {len(loud)} alert(s) in this run</b>",
         "<i>" + (f"all {new_since} are new" if new_since == len(loud) else
                   f"{new_since} new since the previous run")
         + (f", last run {str(last_run)[11:16]} UTC" if last_run else "") + "</i>", ""]

    if total > 0:
        usd_note = "price unavailable"
        money = f"~{total:,.0f} MOCA"
        try:
            sys.path.insert(0, str(ROOT / "detect"))
            import price
            v, usd_note = price.usd(total)
            d = price.fmt_usd(v)
            if d:
                money += f" (~{d})"
            rd = price.fmt_usd(price.usd(rate)[0]) if rate > 0 else None
        except Exception:
            rd = None
        money += f" across {len(per)} wallet(s)"
        if rate > 0:
            money += f", about {rd}/h" if rd else f", about {rate:,.0f} MOCA/h"
        L += [f"<b>Money</b>  {money}", f"<i>{HEDGE} · {usd_note}</i>", ""]

    top = sorted(per.items(), key=lambda kv: -kv[1][0])[:3]
    if top:
        L += ["<b>Top</b>  " + " · ".join(f"<code>{_short(k)}</code> ~{v[0]:,.0f} MOCA" for k, v in top), ""]
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
            L += ["<b>Top</b>  " + " · ".join(names) + "  <i>(no payout total for these — their value is a share, not an amount)</i>", ""]

    if doubled:
        L += [f"<b>Volume</b>  {len(loud)} loud alert(s) this run against {prev_loud} last run — "
              f"it has at least doubled.", ""]

    held = len(loud) - n_shown
    L += [f"Showing {n_shown} of {len(loud)} below"
          + (f"; {held} held, they send in later runs." if held > 0 else "."),
          "The alerts below are <b>silent</b> — this message is the only ping. Sound is kept for: "
          "a signal type not yet seen in this incident, a first cash-out destination, a case you "
          "marked contained firing again, and the volume doubling.", "",
          "<b>Not covered</b>  swaps inside a wallet, anything that leaves Base, anything off-chain, "
          "and who is behind a wallet. Balances are polled, not streamed, so a move can be up to a "
          "run old. Nothing here has been paused or blocked — this bot only informs.", "",
          "Reply to any alert below with <b>reported</b> · <b>contained</b> · <b>watching</b> · "
          "<b>closed</b>. Replying to <i>this</i> message will not match a case."]
    return "\n".join(L)


def send_pending():
    """Send findings marked pending in alerts/state.json (written by detect/run.py)."""
    s = load_state()
    for f in s.get("open", {}).values():                       # clear suppressed ones
        if f.get("pending_send") and _suppressed(f, s):
            f["pending_send"] = False; f["suppressed"] = _suppressed(f, s)
    pending = [f for f in s.get("open", {}).values() if f.get("pending_send")]
    digest = [f for f in pending if f.get("tier") == "digest"]
    loud = [f for f in pending if f.get("tier") != "digest"]

    # ---- incident bookkeeping runs even on an empty slot, so an incident can end
    now = time.time()
    inc, incident_on, doubled = _incident_state(s, len(loud), now)
    prev_loud, last_run = int(s.get("loud_prev") or 0), s.get("last_run_ts")
    if not incident_on and s.get("incident"):
        s.pop("incident", None)
        print("incident: over (no loud findings this run)")

    if not pending:
        s["loud_prev"] = 0; s["last_run_ts"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        save_state(s)
        print("nothing pending"); return 0

    batch = sorted(loud, key=lambda x: 0 if x.get("tier") == "page" else 1)[:INCIDENT_SHOW]

    # ---- the one loud message. If it does NOT deliver, the findings below stay
    # loud: silencing them behind a header nobody received would mute the run.
    header_ok = False
    if incident_on and batch:
        new_since = sum(1 for f in loud if not f.get("last_sent"))
        header = _incident_header(loud, len(batch), new_since, prev_loud, doubled, last_run)
        s["incident"] = inc; save_state(s)               # commit intent BEFORE sending
        r = None
        for i, chunk in enumerate(_chunks(header, 3800)):
            r = send(chunk, silent=bool(i))
        header_ok = bool((r or {}).get("ok"))
        inc["header_ok"] = header_ok
        inc["header_error"] = None if header_ok else str((r or {}).get("error"))[:80]
        save_state(s)
        print(f"incident: header ok={header_ok} loud={len(loud)} shown={len(batch)} doubled={doubled}")
        if not header_ok:
            print("incident: header undelivered — findings below stay loud")
        if header_ok and not inc.get("runs"):
            # Opening run: the header above announces this whole set, so none of
            # these classes is "a signal type not seen this incident" — otherwise
            # the first run of an incident is exactly as loud as no incident mode.
            # A contained case re-firing and a first cash-out still ring; those
            # are separate rules and neither is covered by the header.
            inc["classes"] = sorted({str(f.get("signal") or "?") for f in batch})

    for f in batch:
        reason = _sound_reason(f, inc) if (incident_on and header_ok) else None
        silent = bool(incident_on and header_ok and not reason)
        f["pending_send"] = False; f["last_sent"] = dt.datetime.now(dt.UTC).isoformat()
        f["alert_seq"] = _alert_seq(s, f)                   # footer: "6th alert on this wallet"
        save_state(s)                                       # commit intent BEFORE sending
        body = render(f)
        if reason:
            body += f"\n<i>🔔 Sounding despite incident mode: {reason}.</i>"
        r = send(body, photo=f.get("view_png"), silent=silent)
        f["send_ok"] = bool(r.get("ok")); f["send_error"] = None if r.get("ok") else str(r.get("error"))[:80]
        mid = (r.get("result") or {}).get("message_id")
        if mid:
            f["tg_message_id"] = mid
            s.setdefault("by_message", {})[str(mid)] = f.get("id")   # reply -> case lookup
        if incident_on and r.get("ok"):
            sig = str(f.get("signal") or "?")
            if sig not in (inc.get("classes") or []):
                inc.setdefault("classes", []).append(sig)
            if _is_cashout(f):
                inc["cashout"] = True
        save_state(s)
        print(f["tier"], f.get("signal"), "->", r.get("ok"), "silent" if silent else "loud")

    if incident_on:
        inc["runs"] = int(inc.get("runs") or 0) + 1
        inc["last_ts"] = now
        s["incident"] = inc
    s["loud_prev"] = len(loud)
    s["last_run_ts"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    save_state(s)
    failed = [f for f in s.get("open", {}).values() if f.get("send_ok") is False]
    if failed and not s.get("send_failure_alarmed"):
        s["send_failure_alarmed"] = True; save_state(s)
        send(f"🔴 <b>{len(failed)} alert(s) failed to send</b> — check the run log; findings are in the repo index", silent=False)
    elif not failed and s.get("send_failure_alarmed"):
        s["send_failure_alarmed"] = False; save_state(s)
    if digest:
        for f in digest: f["pending_send"] = False; f["last_sent"] = dt.datetime.now(dt.UTC).isoformat()
        save_state(s)
        try:
            from explain import digest as fmt_digest
        except ImportError:
            from notify.explain import digest as fmt_digest
        body = fmt_digest(digest)
        r = None
        for chunk in _chunks(body, 3800):        # never truncate mid-tag: HTML would 400
            r = send(chunk, silent=True)
        if not (r or {}).get("ok"):
            for f in digest:                     # not delivered -> keep it pending
                f["pending_send"] = True
            print("digest: send failed, left pending")
        save_state(s)
    return 0

def failure(url):
    s = load_state(); last = s.get("last_failure_post", 0); now = time.time()
    if now - last < 6 * 3600 and s.get("last_run_ok", True):
        print("failure post deduped"); return 0
    s["last_failure_post"] = now; s["last_run_ok"] = False; save_state(s)
    send(f"🔴 <b>detector run failed</b>\n{url}", silent=False); return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-pending", action="store_true")
    ap.add_argument("--failure", metavar="URL")
    ap.add_argument("--test", metavar="TEXT")
    a = ap.parse_args()
    if a.failure: sys.exit(failure(a.failure))
    if a.test:    print(send(a.test)); sys.exit(0)
    sys.exit(send_pending())
