#!/usr/bin/env python3
"""Bank every MOCA (Base) ERC-20 Transfer event, politely, resumably.

- Source: public Base RPC eth_getLogs on the MOCA contract (free, no key).
- Window: adaptive (starts 1500 blocks, halves on error, grows back slowly).
- Pace: >= PACE seconds between requests + exponential backoff on 429/5xx.
- Output: data/YYYY-MM-DD.jsonl  (one JSON row per transfer, UTC day by block ts)
- State: state.json {next_block, head_at_start, rows_total}. Re-run to resume / catch up.
Timestamps: Base produces a block every 2 s deterministically -> ts = anchor_ts + 2*(block-anchor).
"""
import json, os, sys, time, urllib.request, datetime as dt

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

def rpc(method, params, timeout=60):
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    last = None
    for attempt in range(8):
        url = RPCS[attempt % len(RPCS)]
        try:
            req = urllib.request.Request(url, data=body, headers={"content-type":"application/json","User-Agent":"Mozilla/5.0 (moca-ledger/1.0; polite crawler)"})
            j = json.load(urllib.request.urlopen(req, timeout=timeout))
            if "result" in j: return j["result"]
            last = j.get("error", {}).get("message", "rpc error")
            if "limit" in last.lower() or "range" in last.lower() or "too many" in last.lower() or "large" in last.lower():
                raise ValueError(last)     # window too big -> let caller shrink
        except ValueError: raise
        except Exception as e:
            last = str(e)
            if "413" in last or "too large" in last.lower(): raise ValueError(last)
        time.sleep(min(60, 2 ** attempt) + PACE)
    raise RuntimeError(f"rpc failed: {method} {last}")

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
    win = st.get("win", 1500); files = {}
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
            win = max(100, win // 2); say(f"shrink win->{win} ({str(e)[:60]})"); time.sleep(PACE); continue
        req += 1
        rows_by_day = {}
        for l in logs:
            if len(l["topics"]) < 3: continue
            bn = int(l["blockNumber"], 16)
            row = {"block": bn, "ts": ts_of(bn), "tx": l["transactionHash"], "li": int(l["logIndex"], 16),
                   "from": "0x"+l["topics"][1][-40:], "to": "0x"+l["topics"][2][-40:], "value": str(int(l["data"], 16))}
            rows_by_day.setdefault(day_of(bn), []).append(json.dumps(row, separators=(",",":")))
        for d, rows in rows_by_day.items():
            if d not in files: files[d] = open(os.path.join(DATA, d + ".jsonl"), "a")
            files[d].write("\n".join(rows) + "\n"); files[d].flush()
        st["rows_total"] += len(logs); nxt = to + 1; st["next_block"] = nxt; st["win"] = win
        if len(logs) < 700 and win < 2000: win = min(2000, win + 100)
        if req % 10 == 0:
            save_state(st); done = nxt - START_BLOCK; rate = (time.time()-t0)/req
            say(f"block {nxt} ({day_of(nxt)}) rows={st['rows_total']} win={win} req={req} {rate:.1f}s/req eta={(target-nxt)/win*rate/60:.0f}m")
        time.sleep(PACE)
    save_state(st); [f.close() for f in files.values()]
    say(f"done: next={nxt} rows_total={st['rows_total']}")

if __name__ == "__main__": main()
