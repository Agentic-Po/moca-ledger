#!/usr/bin/env python3
"""Bank every MOCA (Base) ERC-20 Transfer event, politely, resumably.

- Source: public Base RPC eth_getLogs on the MOCA contract (free, no key).
- Window: adaptive (starts 1500 blocks, halves to one on range error).
- Pace: >= PACE seconds between requests + exponential backoff on 429/5xx.
- Output: data/YYYY-MM-DD.jsonl  (one JSON row per transfer, UTC day by block ts)
- State: state.json {next_block, head_at_start, rows_total}. Re-run to resume / catch up.
Timestamps: Base produces a block every 2 s deterministically -> ts = anchor_ts + 2*(block-anchor).
"""
import json, os, sys, time, urllib.request, urllib.error, datetime as dt

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

def rpc(method, params, timeout=20):
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    # Try every provider before shrinking: one provider's cap must not
    # suppress another provider that can serve the original range.
    last = "unavailable"
    for attempt in range(2):
        limited = False
        for url in RPCS:
            try:
                req = urllib.request.Request(url.strip(), data=body, headers={"content-type":"application/json","User-Agent":"Mozilla/5.0 (moca-ledger/1.0; polite crawler)"})
                try:
                    response = urllib.request.urlopen(req, timeout=timeout)
                    with response:
                        j = json.load(response)
                except urllib.error.HTTPError as e:
                    # Some providers return JSON range errors with HTTP 400.
                    # Do not print response bodies or configured endpoint URLs.
                    last = f"HTTP {e.code}"
                    try:
                        j = json.loads(e.read())
                    except (ValueError, UnicodeError):
                        if e.code == 413:
                            limited = True
                        continue
                if "result" in j:
                    if method == "eth_getLogs" and not isinstance(j["result"], list):
                        last = "invalid log result"
                        continue
                    return j["result"]
                error = j.get("error", {})
                if isinstance(error, dict) and _range_error(str(error.get("message", ""))):
                    limited = True
                last = "provider rejected request"
            except Exception:
                last = "provider unavailable"
        if limited:
            raise ValueError("provider range limit")
        if attempt == 0:
            time.sleep(PACE + 1)
    raise RuntimeError(f"rpc failed: {method} ({last})")

def ts_of(block):  return ANCHOR_TS + 2 * (block - ANCHOR_BLOCK)
def day_of(block): return dt.datetime.fromtimestamp(ts_of(block), dt.UTC).strftime("%Y-%m-%d")

def load_state():
    if os.path.exists(STATE): return json.load(open(STATE))
    return {"next_block": START_BLOCK, "rows_total": 0, "started": dt.datetime.now(dt.UTC).isoformat()}
def save_state(s):
    tmp = STATE + ".tmp"; json.dump(s, open(tmp, "w"), indent=1); os.replace(tmp, STATE)

def main():
    global ANCHOR_BLOCK, ANCHOR_TS
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    head = int(rpc("eth_blockNumber", []), 16)
    b = rpc("eth_getBlockByNumber", [hex(head), False]); ANCHOR_BLOCK, ANCHOR_TS = head, int(b["timestamp"], 16)
    st = load_state(); nxt = st["next_block"]; target = head - CONFIRM
    win = st.get("win", 1500); files = {}; seen = {}
    log = open(os.path.join(HERE, "logs", "crawl.log"), "a")
    def say(m):
        line = f"{dt.datetime.now(dt.UTC).strftime('%H:%M:%S')} {m}"; print(line, flush=True); log.write(line+"\n"); log.flush()
    say(f"start next={nxt} target={target} behind={target-nxt} blocks win={win}")
    if nxt > target: say("up to date"); return
    t0 = time.time(); req = 0
    while nxt <= target:
        to = min(nxt + win - 1, target)
        try:
            logs = rpc("eth_getLogs", [{"fromBlock": hex(nxt), "toBlock": hex(to), "address": TOK, "topics": [TOPIC]}])
        except ValueError as e:
            if win == 1:
                raise RuntimeError("rpc failed: eth_getLogs at minimum range") from None
            win = max(1, win // 2); say(f"shrink win->{win} ({str(e)[:60]})"); time.sleep(PACE); continue
        req += 1
        rows_by_day = {}; event_keys = set()
        for l in logs:
            if (not isinstance(l, dict) or len(l.get("topics", [])) != 3
                    or l["topics"][0].lower() != TOPIC or l.get("removed", False)):
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
                files[d] = open(path, "a")
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
        st["rows_total"] += len(event_keys); nxt = to + 1; st["next_block"] = nxt; st["win"] = win
        if len(logs) < 700 and win < 2000: win = min(2000, win + 100)
        save_state(st)
        if req % 10 == 0:
            done = nxt - START_BLOCK; rate = (time.time()-t0)/req
            say(f"block {nxt} ({day_of(nxt)}) rows={st['rows_total']} win={win} req={req} {rate:.1f}s/req eta={(target-nxt)/win*rate/60:.0f}m")
        time.sleep(PACE)
    save_state(st); [f.close() for f in files.values()]
    say(f"done: next={nxt} rows_total={st['rows_total']}")

if __name__ == "__main__": main()
