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

def send(text, photo=None, silent=False):
    if photo and pathlib.Path(photo).exists():
        r = _post("sendPhoto", {"chat_id": CHAT(), "caption": text[:1024], "parse_mode": "HTML",
                                "disable_notification": "true" if silent else "false"},
                  {"photo": (pathlib.Path(photo).name, pathlib.Path(photo).read_bytes())})
        if r.get("ok"): return r
    return _post("sendMessage", {"chat_id": CHAT(), "text": text[:4096], "parse_mode": "HTML",
                                 "disable_web_page_preview": "true",
                                 "disable_notification": "true" if silent else "false"})

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
        except Exception:
            pass
    return _render_terse(f)


def _render_terse(f):
    """Fallback: the original compact form."""
    i = ICON.get(f.get("tier", "notify"), "🟠")
    head = f"{i} <b>{f.get('tier','notify').upper()} · {f.get('signal')}</b>"
    key  = f.get("key", "")
    lines = [head]
    if key: lines.append(f"entity <code>{key}</code>")
    v, t = f.get("value"), f.get("threshold")
    if v is not None: lines.append(f"value <b>{v}</b> vs threshold {t}" + (f" · organic p95 {f['organic_p95']}" if f.get("organic_p95") is not None else ""))
    if f.get("window"): lines.append(f"window {f['window']}")
    for h in (f.get("headline") or [])[:3]: lines.append(f"• {h}")
    if f.get("recommended_action"): lines.append(f"\n➡️ {f['recommended_action']}")
    if f.get("owner"): lines.append(f"owner: {f['owner']}")
    lines.append(f"\n<code>/ack {f.get('id','')}</code>  ·  as of block {f.get('as_of_block','?')}")
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


def send_pending():
    """Send findings marked pending in alerts/state.json (written by detect/run.py)."""
    s = load_state()
    for f in s.get("open", {}).values():                       # clear suppressed ones
        if f.get("pending_send") and _suppressed(f, s):
            f["pending_send"] = False; f["suppressed"] = _suppressed(f, s)
    pending = [f for f in s.get("open", {}).values() if f.get("pending_send")]
    if not pending:
        print("nothing pending"); return 0
    digest, loud = [f for f in pending if f.get("tier") == "digest"], [f for f in pending if f.get("tier") != "digest"]
    for f in sorted(loud, key=lambda x: 0 if x.get("tier") == "page" else 1)[:6]:
        f["pending_send"] = False; f["last_sent"] = dt.datetime.now(dt.UTC).isoformat()
        save_state(s)                                       # commit intent BEFORE sending
        r = send(render(f), photo=f.get("view_png"))
        f["send_ok"] = bool(r.get("ok")); f["send_error"] = None if r.get("ok") else str(r.get("error"))[:80]
        save_state(s)
        print(f["tier"], f.get("signal"), "->", r.get("ok"))
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
        send(body, silent=True); save_state(s)
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
