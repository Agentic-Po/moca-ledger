"""Read-only saved scan cutoff; never use latest-event or build timestamps."""
import datetime as dt
import json
from pathlib import Path
import time

SGT = dt.timezone(dt.timedelta(hours=8))

def cutoff_lines(root, now=None, header=None, start_block=None):
    now = now or dt.datetime.now(dt.UTC)
    if now.tzinfo is None:
        raise ValueError('explicit UTC observation time required')
    now = now.astimezone(dt.UTC)
    asof = 'As of: ' + now.astimezone(SGT).strftime('%d %b %Y %H:%M SGT')
    try:
        state = json.loads((Path(root) / 'state.json').read_text())
        nxt = state['next_block']
        if type(nxt) is not int or nxt <= 0:
            raise ValueError('invalid saved cursor')
        through = nxt - 1
        if start_block is None:
            import crawl
            start_block = crawl.START_BLOCK
        if through < start_block:
            raise ValueError('no verified scan range saved')
    except (OSError, ValueError, KeyError, TypeError):
        return ['Saved scan cutoff: unavailable (no valid saved cursor)', asof]
    unknown = f'Saved scan cutoff: block {through:,} · time unavailable'
    try:
        if header is None:
            import crawl
            previous = crawl._DEADLINE
            try:
                # Existing providers and response validation only. This read
                # never advances the crawler or writes its saved state.
                crawl._DEADLINE = time.monotonic() + 10
                block = crawl.rpc('eth_getBlockByNumber', [hex(through), False])
            finally:
                crawl._DEADLINE = previous
        else:
            block = header(through)
        if not isinstance(block, dict) or int(block['number'], 16) != through:
            raise ValueError('wrong cutoff block')
        import re
        if not re.fullmatch(r'0x[0-9a-fA-F]{64}', block['hash']):
            raise ValueError('invalid cutoff hash')
        stamp = dt.datetime.fromtimestamp(int(block['timestamp'], 16), dt.UTC)
        if stamp > now or stamp.year < 2023:
            raise ValueError('invalid cutoff timestamp')
        minutes = int((now - stamp).total_seconds() // 60)
        when = stamp.astimezone(SGT).strftime('%d %b %Y %H:%M SGT')
        return [f'Saved scan cutoff: {when} · {minutes // 60}h {minutes % 60:02d}m behind',
                f'Saved block: {through:,}', asof]
    except Exception:
        return [unknown, asof]
