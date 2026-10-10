#!/usr/bin/env python3
"""Bank every MOCA (Base) ERC-20 Transfer event, politely, resumably.

- Source: public Base RPC eth_getLogs on the MOCA contract (free, no key).
- Window: adaptive (starts 1500 blocks, halves to one on range error).
- Pace: >= PACE seconds between requests + exponential backoff on 429/5xx.
- Output: data/YYYY-MM-DD.jsonl  (one JSON row per transfer, UTC day by block ts)
- State: state.json {next_block, head_at_start, rows_total}. Re-run to resume / catch up.
Timestamps: Base produces a block every 2 s deterministically -> ts = anchor_ts + 2*(block-anchor).
"""
import json, os, sys, time, re, contextlib, urllib.request, urllib.error, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
RPCS = [u for u in (os.environ.get("BASE_RPCS") or
        "https://mainnet.base.org,https://base.publicnode.com,https://base-rpc.publicnode.com,https://1rpc.io/base,https://base.drpc.org").split(",") if u.strip()]
TOK  = "0x2b11834ed1feaed4b4b3a86a6f571315e25a884d"   # MOCA on Base
TOPIC= "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
START_BLOCK = int(os.environ.get("START_BLOCK", "48270000"))   # ~2026-07-06 UTC, buffer before first MOCA cognition flows (Jul 11)
PACE  = float(os.environ.get("PACE", "1.5"))
CONFIRM = 30           # stay this many blocks behind head (reorg safety)
STATE = os.path.join(HERE, "state.json")
DATA  = os.path.join(HERE, "data")
ANCHOR_BLOCK, ANCHOR_TS = 50263273, 1787307825   # verified pair (block seen at 2026-08-21 ~12:23 UTC); refined on start

def _range_error(message):
    message = message.lower()
    if any(word in message for word in ("rate limit", "rate-limit", "too many requests", "quota", "per second", "per minute", "per day", "unauthorized", "forbidden")):
        return False
    return ("range" in message or "too many results" in message
            or "response size" in message or "too large" in message
            or ("limit" in message and ("block" in message or "result" in message)))

class CrawlBudgetExceeded(TimeoutError):
    pass

_DEADLINE = None
PARTIAL_EXIT = 75

def _remaining():
    if _DEADLINE is None:
        return None
    remaining = _DEADLINE - time.monotonic()
    if remaining <= 0:
        raise CrawlBudgetExceeded("crawl budget exhausted")
    return remaining

def _pause(seconds):
    remaining = _remaining()
    if remaining is not None and remaining <= seconds:
        raise CrawlBudgetExceeded("crawl budget exhausted")
    time.sleep(seconds)

_PREFERRED = {}
_DENIED = set()
_COOLDOWN = {}
LOG_CYCLE_SECONDS = 45
LOG_SOCKET_SECONDS = 8
CHECKPOINT_MARGIN_SECONDS = 5  # reserve for local persistence, not a hard fsync timeout

def _hex(value, digits=None, quantity=False):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
        return False
    if digits is not None and len(value) != digits + 2:
        return False
    return not quantity or value == "0x0" or value[2] != "0"

def _valid_result(method, value, params):
    if method == "eth_getLogs":
        if not isinstance(value, list) or not all(_transfer_valid(log) for log in value):
            return False
        if value:
            if not params or not isinstance(params[0], dict):
                return False
            lower = int(params[0]["fromBlock"], 16)
            upper = int(params[0]["toBlock"], 16)
            if any(not lower <= int(log["blockNumber"], 16) <= upper for log in value):
                return False
        return True
    if method == "eth_blockNumber":
        return _hex(value, quantity=True)
    if method == "eth_getBlockByNumber":
        return (isinstance(value, dict) and _hex(value.get("number"), quantity=True)
                and value["number"].lower() == params[0].lower()
                and _hex(value.get("timestamp"), quantity=True)
                and _hex(value.get("hash"), 64))
    return False

def _transfer_valid(log):
    if not isinstance(log, dict):
        return False
    topics = log.get("topics")
    return (isinstance(topics, list) and len(topics) == 3
            and all(_hex(topic, 64) for topic in topics)
            and topics[0].lower() == TOPIC
            and all(topic[2:26] == "0" * 24 for topic in topics[1:])
            and isinstance(log.get("address"), str) and log["address"].lower() == TOK
            and log.get("removed") is False
            and _hex(log.get("blockNumber"), quantity=True)
            and _hex(log.get("blockHash"), 64)
            and _hex(log.get("transactionHash"), 64)
            and _hex(log.get("logIndex"), quantity=True)
            and _hex(log.get("data"), 64))


def rpc(method, params, timeout=20):
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    # Try every provider before shrinking: one provider's cap must not
    # suppress another provider that can serve the original range.
    last = "unavailable"
    cycle_deadline = time.monotonic() + LOG_CYCLE_SECONDS if method == "eth_getLogs" else None
    for attempt in range(2):
        limited = False
        preferred = _PREFERRED.get(method)
        providers = ([preferred] if preferred in RPCS else []) + [u for u in RPCS if u != preferred]
        for url in providers:
            key = (method, url)
            if key in _DENIED or _COOLDOWN.get(key, 0) > time.monotonic():
                continue
            try:
                remaining = _remaining()
            except CrawlBudgetExceeded:
                raise RuntimeError(f"rpc failed: {method} (budget before verified response)") from None
            request_timeout = timeout if remaining is None else min(timeout, remaining)
            if cycle_deadline is not None:
                cycle_remaining = cycle_deadline - time.monotonic()
                if cycle_remaining <= 0:
                    raise RuntimeError(f"rpc failed: {method} (request cycle exhausted)")
                request_timeout = min(request_timeout, LOG_SOCKET_SECONDS, cycle_remaining)
            try:
                req = urllib.request.Request(url.strip(), data=body, headers={"content-type":"application/json","User-Agent":"Mozilla/5.0 (moca-ledger/1.0; polite crawler)"})
                try:
                    response = urllib.request.urlopen(req, timeout=request_timeout)
                    with response:
                        j = json.load(response)
                except urllib.error.HTTPError as e:
                    # Some providers return JSON range errors with HTTP 400.
                    # Do not print response bodies or configured endpoint URLs.
                    last = f"HTTP {e.code}"
                    if e.code == 413:
                        limited = True
                    if e.code == 403:
                        _DENIED.add(key)
                        continue
                    if e.code == 429:
                        _COOLDOWN[key] = time.monotonic() + 30
                        continue
                    try:
                        j = json.loads(e.read())
                        error = j.get("error") if isinstance(j, dict) else None
                        if isinstance(error, dict) and isinstance(error.get("message"), str) and _range_error(error["message"]):
                            limited = True
                        continue
                    except (ValueError, UnicodeError):
                        if e.code == 413:
                            limited = True
                        continue
                if (not isinstance(j, dict) or j.get("jsonrpc") != "2.0"
                        or type(j.get("id")) is not int or j["id"] != 1
                        or ("result" in j) == ("error" in j)):
                    last = "invalid response envelope"
                    continue
                if "result" in j:
                    if not _valid_result(method, j["result"], params):
                        last = "invalid result"
                        continue
                    if cycle_deadline is not None and time.monotonic() >= cycle_deadline:
                        raise RuntimeError("request cycle expired before verified response")
                    _remaining()
                    _PREFERRED[method] = url
                    return j["result"]
                error = j.get("error", {})
                if (isinstance(error, dict) and type(error.get("code")) is int
                        and isinstance(error.get("message"), str) and _range_error(error["message"])):
                    limited = True
                last = "provider rejected request"
            except CrawlBudgetExceeded:
                raise RuntimeError(f"rpc failed: {method} (budget before verified response)") from None
            except Exception:
                last = "provider unavailable"
        if limited:
            raise ValueError("provider range limit")
        if attempt == 0:
            if cycle_deadline is not None and cycle_deadline - time.monotonic() <= PACE + 1:
                raise RuntimeError(f"rpc failed: {method} (request cycle exhausted)")
            try:
                _pause(PACE + 1)
            except CrawlBudgetExceeded:
                raise RuntimeError(f"rpc failed: {method} (budget after provider failure)") from None
    raise RuntimeError(f"rpc failed: {method} ({last})")

def ts_of(block):  return ANCHOR_TS + 2 * (block - ANCHOR_BLOCK)
def day_of(block): return dt.datetime.fromtimestamp(ts_of(block), dt.UTC).strftime("%Y-%m-%d")

def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"next_block": START_BLOCK, "rows_total": 0, "started": dt.datetime.now(dt.UTC).isoformat()}
def save_state(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)

def _crawl(stack):
    global ANCHOR_BLOCK, ANCHOR_TS
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    head = int(rpc("eth_blockNumber", []), 16)
    b = rpc("eth_getBlockByNumber", [hex(head), False]); ANCHOR_BLOCK, ANCHOR_TS = head, int(b["timestamp"], 16)
    st = load_state(); nxt = st["next_block"]; target = head - CONFIRM
    ceiling = st.get("range_ceiling")
    win = min(st.get("win", 1500), ceiling or 2000); files = {}; seen = {}
    range_rejected = False
    log = stack.enter_context(open(os.path.join(HERE, "logs", "crawl.log"), "a"))
    def say(m):
        line = f"{dt.datetime.now(dt.UTC).strftime('%H:%M:%S')} {m}"; print(line, flush=True); log.write(line+"\n"); log.flush()
    say(f"start next={nxt} target={target} behind={target-nxt} blocks win={win}")
    if nxt > target:
        say("up to date")
        log.close()
        return
    def partial():
        for f in files.values():
            f.flush()
            os.fsync(f.fileno())
            f.close()
        save_state(st)
        say("crawl checkpoint saved; catch-up incomplete")
        log.close()
        return PARTIAL_EXIT
    t0 = time.time(); req = 0
    while nxt <= target:
        to = min(nxt + win - 1, target)
        try:
            remaining = _remaining()
            if not range_rejected and remaining is not None and remaining <= LOG_CYCLE_SECONDS + CHECKPOINT_MARGIN_SECONDS:
                return partial()
            logs = rpc("eth_getLogs", [{"fromBlock": hex(nxt), "toBlock": hex(to), "address": TOK, "topics": [TOPIC]}])
        except CrawlBudgetExceeded:
            if range_rejected:
                raise RuntimeError("rpc failed: eth_getLogs (budget after range refusal)") from None
            return partial()
        except ValueError as e:
            if win == 1:
                raise RuntimeError("rpc failed: eth_getLogs at minimum range") from None
            range_rejected = True
            win = max(1, win // 2); say(f"shrink win->{win} ({str(e)[:60]})")
            try:
                _pause(PACE)
            except CrawlBudgetExceeded:
                raise RuntimeError("rpc failed: eth_getLogs (budget after range refusal)") from None
            continue
        req += 1
        rows_by_day = {}; event_keys = set()
        for l in logs:
            if not _transfer_valid(l):
                raise RuntimeError("rpc returned invalid Transfer event")
            bn = int(l["blockNumber"], 16)
            if not nxt <= bn <= to:
                raise RuntimeError("rpc returned event outside requested range")
            row = {"block": bn, "ts": ts_of(bn), "tx": l["transactionHash"], "li": int(l["logIndex"], 16),
                   "from": "0x"+l["topics"][1][-40:], "to": "0x"+l["topics"][2][-40:], "value": str(int(l["data"], 16))}
            key = (row["tx"], row["li"])
            if key not in event_keys:
                event_keys.add(key)
                rows_by_day.setdefault(day_of(bn), []).append(row)
        for d, rows in rows_by_day.items():
            if d not in files:
                path = os.path.join(DATA, d + ".jsonl")
                # A process may stop after an append but before its checkpoint.
                # Replay the same range without appending its events twice.
                seen[d] = set()
                if os.path.exists(path):
                    with open(path) as prior:
                        for line in prior:
                            old = json.loads(line)
                            if old["block"] >= nxt:
                                seen[d].add((old["tx"], old["li"]))
                files[d] = stack.enter_context(open(path, "a"))
            fresh = []
            for row in rows:
                key = (row["tx"], row["li"])
                if key not in seen[d]:
                    fresh.append(json.dumps(row, separators=(",",":")))
                    seen[d].add(key)
            if fresh:
                files[d].write("\n".join(fresh) + "\n")
                files[d].flush()
                os.fsync(files[d].fileno())
        if range_rejected:
            ceiling = win
            st["range_ceiling"] = ceiling
            range_rejected = False
        st["rows_total"] += len(event_keys); nxt = to + 1; st["next_block"] = nxt; st["win"] = win
        if len(logs) < 700 and win < (ceiling or 2000): win = min(ceiling or 2000, win + 100)
        save_state(st)
        if req % 10 == 0:
            done = nxt - START_BLOCK; rate = (time.time()-t0)/req
            say(f"block {nxt} ({day_of(nxt)}) rows={st['rows_total']} win={win} req={req} {rate:.1f}s/req eta={(target-nxt)/win*rate/60:.0f}m")
        if nxt <= target:
            try:
                _pause(PACE)
            except CrawlBudgetExceeded:
                return partial()
    save_state(st); [f.close() for f in files.values()]
    say(f"done: next={nxt} rows_total={st['rows_total']}")
    log.close()

def _main():
    with contextlib.ExitStack() as stack:
        return _crawl(stack)

def main():
    global _DEADLINE
    budget = float(os.environ.get("CRAWL_BUDGET_SECONDS", "240"))
    if not 1 <= budget <= 240:
        raise ValueError("crawl budget must be between 1 and 240 seconds")
    prior = _DEADLINE
    _PREFERRED.clear(); _DENIED.clear(); _COOLDOWN.clear()
    _DEADLINE = time.monotonic() + budget
    try:
        return _main()
    finally:
        _DEADLINE = prior

if __name__ == "__main__": sys.exit(main())
