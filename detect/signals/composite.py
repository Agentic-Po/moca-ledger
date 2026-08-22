"""Composite page rule: >= 2 distinct notify-or-page signals open on the same or
linked wallet entities within 6 h -> PAGE.

Linked = same wallet, or a direct ledger transfer edge between the two wallets at
any point up to the fire time. Platform-keyed findings and digest-tier fires are
excluded; S-G and #13 are excluded as members (watchlist touches and outflow
scores would otherwise chain historic entities together — the composite is meant
to catch two independent *shape* signals agreeing).
"""
import collections
import hashlib
from . import register, Finding, SLOT, H, utc

MEMBERS = {"10", "10n", "11", "4b", "S-C", "S-A", "S-Q2"}


@register("composite", order=90)
def run(ctx):
    T = ctx.thr
    win = T["composite_window_h"] * H
    events = []   # (ts, signal, entity)
    for sid, lst in ctx.fires.items():
        if sid not in MEMBERS:
            continue
        for f in lst:
            if f.tier in ("notify", "page") and f.key != "platform":
                events.append((f.ts, sid, f.key))
    if not events:
        return []
    events.sort()
    # linkage: direct transfer edges between involved entities (either direction)
    involved = {e for _, _, e in events}
    edges = collections.defaultdict(set)   # wallet -> linked wallets, with earliest edge ts
    edge_ts = {}
    for ts, f, t, v, tx, b in ctx.rows:
        if f in involved and t in involved and f != t:
            k = (min(f, t), max(f, t))
            if k not in edge_ts:
                edge_ts[k] = ts
                edges[f].add(t)
                edges[t].add(f)

    def linked(a, b, at_ts):
        if a == b:
            return True
        k = (min(a, b), max(a, b))
        return k in edge_ts and edge_ts[k] <= at_ts

    fires = []
    fired_sets = set()
    for i, (ts, sid, ent) in enumerate(events):
        group = {(sid, ent)}
        ents = {ent}
        j = i - 1
        while j >= 0 and events[j][0] >= ts - win:
            ts2, sid2, ent2 = events[j]
            if sid2 != sid and any(linked(ent2, e, ts) for e in ents):
                group.add((sid2, ent2))
                ents.add(ent2)
            j -= 1
        sids = {s for s, _ in group}
        if len(sids) >= T["composite_min_signals"]:
            keyhash = hashlib.sha256("|".join(sorted(ents)).encode()).hexdigest()[:10]
            if keyhash in fired_sets:
                continue
            fired_sets.add(keyhash)
            fires.append(Finding("composite", keyhash, "page", len(sids), T["composite_min_signals"],
                                 window="6h", ts=ts,
                                 headline=[f"{len(sids)} distinct signals on linked entities within 6 h: "
                                           + ", ".join(sorted(sids)),
                                           f"{len(ents)} linked entities"],
                                 detail=", ".join(sorted(sids)),
                                 evidence=[(s, e[:14]) for s, e in sorted(group)]))
    return fires
