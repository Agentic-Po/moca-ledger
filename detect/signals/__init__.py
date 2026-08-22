"""Signal engine for the public detection floor.

Every signal module in this package exposes one or more functions decorated with
@register("<signal-id>"); each returns a list of Finding fire events for the
whole evaluated range (one per slot per entity, edge-deduped by the reducer).
`detect/run.py` (live) and `detect/replay.py` (replay) both call evaluate() on
the exact same modules, so the two cannot drift.

All windows are rolling and evaluated on 10-minute slot boundaries — never
UTC-hour buckets. All timestamps come from the ledger itself; nothing here
reads the wall clock.
"""
import bisect
import collections
import glob
import gzip
import hashlib
import json
import os
import statistics
import datetime as dt
from dataclasses import dataclass, field, asdict

SLOT = 600
H = 3600
DAY = 86400
WEEK = 7 * DAY
E18 = 10 ** 18

HERE = os.path.dirname(os.path.abspath(__file__))
DETECT = os.path.dirname(HERE)
ROOT = os.path.dirname(DETECT)

TREASURY = "0xbd956171f5b50936f0ad1c4db80c022bd2442519"
QUEST = "0xb15afc65532f8ec4d39db521ad7eb5b9e9ef5acf"

ACTION = {  # neutral wording only; entity-level asks live in the private layer
    "page": "request reward pause for this recipient and start a review",
    "notify": "review this entity; add to watch if the pattern repeats",
    "digest": "no action; context only",
}


def ts_of(s):
    s = s.replace("Z", "")
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC).timestamp())


def utc(ts):
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d %H:%M")


def day_str(ts):
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- Finding
@dataclass
class Finding:
    signal: str                 # e.g. "10", "11", "S-Q2"
    key: str                    # entity: wallet | "platform" | "platform:<band>"
    tier: str                   # page | notify | digest
    value: object = None
    threshold: object = None
    organic_p95: object = None
    window: str = ""
    ts: int = 0                 # fire time (slot end, ledger time)
    headline: list = field(default_factory=list)   # up to 3 strings
    recommended_action: str = ""
    owner: str = ""   # filled from thresholds.escalation_owner at build time
    as_of_block: int = 0
    evidence: list = field(default_factory=list)   # ledger rows / tuples
    view_png: str = None
    detail: str = ""
    escalation: str = ""        # e.g. "confirmed-n100", "tier-up"

    @property
    def id(self):
        return hashlib.sha256(f"{self.signal}|{self.key}".encode()).hexdigest()[:10]

    def to_state(self):
        d = asdict(self)
        d["id"] = self.id
        d["first_ts"] = utc(self.ts)
        d["evidence"] = None  # evidence lives in the incident folder, not in state
        return d


REGISTRY = []  # (order, signal-group name, fn)


def register(name, order=50):
    def deco(fn):
        REGISTRY.append((order, name, fn))
        return fn
    return deco


# ---------------------------------------------------------------- thresholds
def load_thresholds():
    with open(os.path.join(DETECT, "thresholds.json")) as fh:
        thr = json.load(fh)
    env = os.environ.get("THRESHOLDS_JSON")
    if env:
        try:
            thr.update(json.loads(env))
        except Exception:
            pass  # bad override never kills the run; defaults stand
    return thr


# ---------------------------------------------------------------- ledger + config loading
def load_ledger(path, as_of_block=None):
    rows = []
    files = sorted(glob.glob(os.path.join(path, "*.jsonl"))) + sorted(glob.glob(os.path.join(path, "**", "*.jsonl.gz"), recursive=True))
    for f in sorted(files):
        op = gzip.open if f.endswith(".gz") else open
        with op(f, "rt") as fh:
            for line in fh:
                r = json.loads(line)
                if as_of_block is not None and r["block"] > as_of_block:
                    continue
                rows.append((r["block"], r["li"], r["ts"], r["from"].lower(), r["to"].lower(), int(r["value"]), r["tx"].lower()))
    rows.sort()
    return [(ts, f, t, v / E18, tx, b) for (b, li, ts, f, t, v, tx) in rows]


def _salt():
    s = os.environ.get("MINDSET_SALT", "")
    if not s:
        p = os.path.join(os.path.expanduser("~"), ".moca-ledger", "mindset_salt")
        if os.path.exists(p):
            s = open(p).read().strip()
    return s


class Ctx:
    """Everything the signals need, precomputed once."""

    def __init__(self, root=ROOT, as_of_block=None, thresholds=None, data_dir=None):
        self.thr = thresholds or load_thresholds()
        self.rows = load_ledger(data_dir or os.path.join(root, "data"), as_of_block)
        if not self.rows:
            raise SystemExit("empty ledger")
        self.t0, self.t1 = self.rows[0][0], self.rows[-1][0]
        self.as_of_block = self.rows[-1][5]
        self.s0, self.s1 = self.t0 // SLOT, self.t1 // SLOT
        self.slots = range(self.s0, self.s1 + 1)
        self.fires = {}          # signal id -> [Finding]; filled by evaluate()

        allow_doc = json.load(open(os.path.join(root, "labels", "allowlist.json")))
        self.allow = {e["address"].lower() for e in allow_doc["system"]}
        self.excluded_hashed = set(allow_doc.get("excluded_hashed", []))

        cal = json.load(open(os.path.join(root, "labels", "calendar.json")))
        self.calendar = [dict(e, s=ts_of(e["start"]), e=ts_of(e["end"]) if e["end"] else 4102444800) for e in cal["events"]]
        self.pauses = [c for c in self.calendar if c["kind"] == "reward_pause"]
        self.pause_ts = min((c["s"] for c in self.pauses), default=None)

        lite = json.load(open(os.path.join(root, "labels", "labels-lite.json")))
        self.lite = lite.get("entities", {})

        wl_path = os.path.join(root, "detect", "watchlist.json")
        self.watch_hashed = set()
        if os.path.exists(wl_path):
            self.watch_hashed = set(json.load(open(wl_path)).get("addresses", []))

        self.salt = _salt()
        self._hcache = {}

        # ---- Mind set: salted-hash mindset.json UNION chain (Treasury/QUEST receipts)
        self.mindset_source = "chain-only"
        self.mindset_age_h = None
        mindset_hours = {}
        ms_path = os.path.join(root, "detect", "mindset.json")
        if self.salt and os.path.exists(ms_path):
            ms = json.load(open(ms_path))
            gen = ts_of(ms.get("generated_at", "1970-01-01T00:00"))
            stale_h = ms.get("stale_after_h", 24)
            age_h = max(0.0, (self.t1 - gen) / H)
            self.mindset_age_h = round(age_h, 1)
            if age_h <= stale_h:
                self.mindset_source = "hashed"
            else:
                self.mindset_source = "hashed-stale"
            for h16, hour in ms.get("minds", {}).items():
                try:
                    mindset_hours[h16] = ts_of(hour.replace(" ", "T") + ":00")
                except Exception:
                    pass
        self._mindset_hours = mindset_hours

        # chain part: first Treasury/QUEST receipt makes a wallet a Mind from that ts
        self.mind_from = {}
        self.first_seen = {}
        self.quest_from = {}
        for ts, f, t, v, tx, b in self.rows:
            if f in (TREASURY, QUEST):
                if t not in self.mind_from or ts < self.mind_from[t]:
                    self.mind_from[t] = ts
                if f == QUEST:
                    self.quest_from.setdefault(t, ts)
            for w in (f, t):
                self.first_seen.setdefault(w, ts)
        if mindset_hours:
            for w in list(self.first_seen):
                hts = mindset_hours.get(self.h16(w))
                if hts is not None and (w not in self.mind_from or hts < self.mind_from[w]):
                    self.mind_from[w] = hts

        # ---- Treasury payouts + size bands (trailing-7-day unit, never same-day)
        self.unit_by_day, self.unit_source = self._units()
        self.pay = []
        for ts, f, t, v, tx, b in self.rows:
            if f == TREASURY:
                self.pay.append((ts, t, v, self.band(v, ts), tx))
        self.equips = [p for p in self.pay if p[3] == "equip"]

        base = json.load(open(os.path.join(root, "detect", "baselines.json")))
        self.baselines = base.get("baselines", base)

    # ---- helpers
    def h16(self, addr):
        if not self.salt:
            return None
        v = self._hcache.get(addr)
        if v is None:
            v = hashlib.sha256((self.salt + addr.lower()).encode()).hexdigest()[:16]
            self._hcache[addr] = v
        return v

    def is_internal(self, addr):
        return self.salt and self.h16(addr) in self.excluded_hashed

    def lite_class(self, addr):
        h = self.h16(addr)
        return self.lite.get(h) if h else None

    def on_watch(self, addr):
        return self.salt and self.h16(addr) in self.watch_hashed

    def is_mind(self, w, ts):
        return w not in self.allow and w in self.mind_from and self.mind_from[w] <= ts

    def age_h(self, w, ts):
        return (ts - self.mind_from.get(w, self.first_seen.get(w, ts))) / H

    def in_pause(self, ts):
        return any(c["s"] <= ts < c["e"] for c in self.pauses)

    def cal_exempt(self, ts, kind="admin_credit_batch"):
        return any(c["kind"] == kind and c["s"] <= ts < c["e"] for c in self.calendar)

    def _units(self):
        """Per-day equip unit = median of size-band samples from the trailing 7 full days
        (never same-day: replay-harness daily_unit was look-ahead). Frozen default from
        thresholds.json covers the first days and any empty (post-pause) window."""
        samples = collections.defaultdict(list)
        for ts, f, t, v, tx, b in self.rows:
            if f != TREASURY:
                continue
            if 9 <= v <= 17:
                samples[day_str(ts)].append(v * 10)
            elif 90 <= v <= 175:
                samples[day_str(ts)].append(v)
            elif 280 <= v <= 520:
                samples[day_str(ts)].append(v / 3)
        days = sorted({day_str(r[0]) for r in self.rows} | set(samples))
        unit = {}
        frozen = float(self.thr.get("unit_frozen", 115.0))
        last = frozen
        source = {}
        for i, d in enumerate(days):
            window = []
            d_ts = ts_of(d + "T00:00")
            for j in range(1, 8):
                pd = day_str(d_ts - j * DAY)
                window.extend(samples.get(pd, []))
            if window:
                last = statistics.median(window)
                source[d] = "trailing7d"
            else:
                source[d] = "frozen"
            unit[d] = last
        return unit, source

    def unit(self, ts):
        return self.unit_by_day.get(day_str(ts), float(self.thr.get("unit_frozen", 115.0)))

    def band(self, v, ts):
        u = self.unit(ts)
        if not u:
            return "other"
        r = v / u
        if 0.07 <= r <= 0.16:
            return "invoke"
        if 0.8 <= r <= 1.25:
            return "equip"
        if 2.5 <= r <= 3.6:
            return "airdrop"
        return "other"


# ---------------------------------------------------------------- evaluate + reduce
def evaluate(ctx):
    """Run every registered signal in order; composite (order 90) sees earlier fires."""
    # import all signal modules so they register (idempotent)
    from . import concentration, burst, worker, fanin, slow_harvest, watchlist, quest, velocity, pause, exit_score, outflow, composite  # noqa: F401
    for order, name, fn in sorted(REGISTRY, key=lambda x: x[0]):
        fires = fn(ctx) or []
        for f in fires:
            f.as_of_block = ctx.as_of_block
            if not f.recommended_action:
                f.recommended_action = ACTION.get(f.tier, "")
            ctx.fires.setdefault(f.signal, []).append(f)
    for sid in ctx.fires:
        ctx.fires[sid].sort(key=lambda f: (f.ts, f.key))
    return ctx.fires


def episodes(fires, gap_slots=6):
    """Edge-triggered episodes per entity: runs of fire slots with gaps <= 1 h.
    Returns [(entity, first Finding, last Finding, n_fires, max_tier)]."""
    rank = {"digest": 0, "notify": 1, "page": 2}
    by_ent = collections.defaultdict(list)
    for f in sorted(fires, key=lambda f: f.ts):
        by_ent[f.key].append(f)
    out = []
    for ent, lst in by_ent.items():
        cur = [lst[0]]
        for f in lst[1:]:
            if f.ts // SLOT - cur[-1].ts // SLOT > gap_slots:
                out.append(cur)
                cur = []
            cur.append(f)
        out.append(cur)
    res = []
    for ep in out:
        top = max(ep, key=lambda f: rank.get(f.tier, 0))
        res.append((ep[0].key, ep[0], ep[-1], len(ep), top.tier))
    return sorted(res, key=lambda x: x[1].ts)


def summary(ctx, per_day=True):
    """Per-signal summary used by both replay.py and the parity gate."""
    out = {}
    span_days = max(1.0, (ctx.t1 - ctx.t0) / DAY)
    for sid, fires in sorted(ctx.fires.items()):
        eps = episodes(fires)
        first_page = min((f.ts for f in fires if f.tier == "page"), default=None)
        out[sid] = {
            "fires": len(fires),
            "episodes": len(eps),
            "first_fire": utc(fires[0].ts) if fires else None,
            "first_page": utc(first_page) if first_page else None,
            "fires_per_day": round(len(fires) / span_days, 2),
            "episodes_per_day": round(len(eps) / span_days, 3),
        }
    return out
