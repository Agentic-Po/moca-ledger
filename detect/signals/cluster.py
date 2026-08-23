"""S-B cluster-level concentration, on a key the public floor can compute.

#10 already asks a group question for up to three creators (conc_top3_page, 0.60)
and is firing at top3=99% in the very slot this signal first fires. What it cannot
do is span FOUR or more wallets, or group wallets that never appear in the same
top-3: past three members the only thing that puts a split farm back together is
where the money goes. This groups creators by that, then measures the group
(HANDOFF section 6, item 4).

The grouping key is the one key in the design of record (phase3-build-plan, S-B:
"shared consolidation sink within 24 h (chain key)") that needs no warehouse
join: two creators are in the same cluster when MOCA from both reaches the same
address within two forward hops inside the edge window. The other three S-B keys
need the warehouse join and stay in the private layer. When that layer runs it
must contribute keys to THIS signal rather than emit a second S-B: finding id is
sha256(signal|key), so two S-B producers would silently overwrite each other's
cases.

NOTIFY only, and measured, not assumed: on the only incident in the corpus this
fires 2026-08-21 09:20, which is 44 h AFTER #10 first paged, so it adds no lead
there. It has never been exercised against the evasion it was built for. That is
exactly the new-and-uncalibrated case the fix-round critic reserves shadow mode
for, and it is why this does not page.
"""
import bisect
import collections
import hashlib

from . import register, Finding, SLOT, DAY, H

SINKS_NAMED = 3   # how many collecting wallets ride in `detail` for the copy layer


def shared_sinks(edges, creators):
    """{sink: {creators that reach it in <= 2 forward hops}} over `edges`.

    `edges` must already have allow-listed addresses removed at BOTH ends: the
    Treasury pays every creator and the cognition sink 0xd850 is paid by every
    Mind that invokes, so leaving either in makes one cluster of the platform.
    An early council built its best signal on the quest reward wallet 0xb15a by
    mistake; that wallet is allow-listed as `reward_source` and is stripped here
    with the rest.
    """
    adj = collections.defaultdict(list)
    for ts, f, t in edges:
        adj[f].append((ts, t))
    reach = collections.defaultdict(set)
    for c in creators:
        for ts1, d in adj.get(c, ()):
            if d == c:
                continue
            reach[d].add(c)
            for ts2, e in adj.get(d, ()):
                # Money has to leave d AFTER it arrived. Without this an unrelated
                # earlier payment out of d links two creators that only ever used
                # the same counterparty, at different times, for different reasons.
                if ts2 < ts1 or e == c or e == d:
                    continue
                reach[e].add(c)
    return {s: cs for s, cs in reach.items() if len(cs) >= 2}


def cluster_map(sinks):
    """(creator -> cluster root, cluster root -> sinks) from shared_sinks().

    Union-find over sorted input, root re-mapped to the smallest member: the same
    ledger has to produce the same cluster names in every process, or run.py and
    replay.py disagree and the CI parity gate fails. Nothing here may depend on
    set iteration order, which PYTHONHASHSEED changes between processes.
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s in sorted(sinks):
        cs = sorted(sinks[s])
        for x in cs[1:]:
            ra, rb = find(cs[0]), find(x)
            if ra != rb:
                parent[rb] = ra
    groups = collections.defaultdict(list)
    for c in sorted(parent):
        groups[find(c)].append(c)
    root_of = {}
    for ms in groups.values():
        canon = min(ms)
        for c in ms:
            root_of[c] = canon
    sinks_of = collections.defaultdict(list)
    for s in sorted(sinks):
        sinks_of[root_of[min(sinks[s])]].append((-len(sinks[s]), s))
    return root_of, {r: [s for _, s in sorted(v)] for r, v in sinks_of.items()}


def cluster_id(sink_addrs):
    """The entity key: a hash of the sink set, not of the member set.

    The sinks are the stable half - members join and leave a live farm slot by
    slot, and keying on them would re-open a new case every time one did. It is
    a hash because the design of record says the public floor carries a cluster
    id only; the collecting addresses themselves ride in `detail`, where the
    copy layer can name them, because an address nobody can read is an alert
    nobody can act on.
    """
    return hashlib.sha256("|".join(sorted(sink_addrs)).encode()).hexdigest()[:10]


def fires_share(ck, nn, mk, n_min, top1):
    """The 6 h rule: the cluster is over the concentration line and no single
    member is. A member over it on its own is already #10's finding, and firing
    here as well would count the same money twice."""
    return nn >= n_min and ck / nn >= top1 and mk / nn < top1


def fires_aggregate(ck, mk, agg_min, member_max):
    """The 24 h rule: the cluster clears the aggregate floor while every member
    stays under the per-creator floor S-A uses. This is the spread-across-N
    evasion itself, and the corpus contains no example of it - the August
    operator never bothered to stay small - so CI drives this predicate with
    synthetic counts instead of pretending the ledger exercises it."""
    return ck >= agg_min and mk < member_max


def _named(sinks):
    """The collecting addresses the copy layer is allowed to print, capped so one
    pathological cluster cannot push alerts/state.json toward its byte ceiling."""
    return ",".join(sinks[:SINKS_NAMED])


def _finding(window, key, value, members, sinks, ts, headline, detail):
    """Members ride in `evidence` (incidents/ is gitignored) and never in the copy:
    a group message that lists N creator wallets is a forwarded accusation, and
    nothing here has established that the wallets share an owner."""
    return Finding("S-B", key, "notify", value, None, window=window, ts=ts,
                   headline=headline, detail=detail,
                   evidence=[("member", m, n) for m, n in members] + [("sink", s, "") for s in sinks])


@register("cluster", order=27)
def run(ctx):
    T = ctx.thr
    fires = []
    edge_win = int(T["sb_edge_window_d"]) * DAY
    refresh = int(T["sb_recluster_slots"])
    top1 = T["conc_top1_page"]
    n_min = T["conc_min_n_page"]
    # S-A's OWN bar, not the raw floor. That bar is adaptive; a cluster rule gated on
    # thresholds.sa_floor would leave every member sitting between the two invisible to
    # both signals, which is exactly the evasion this signal exists for.
    from .slow_harvest import aged_floor
    sa_floor = aged_floor(ctx, ctx.t1)
    agg_min = T["sb_mult"] * sa_floor

    edges = [(ts, f, t) for ts, f, t, v, tx, b in ctx.rows
             if f != t and f not in ctx.allow and t not in ctx.allow
             and not ctx.is_internal(f) and not ctx.is_internal(t)]
    edge_ts = [e[0] for e in edges]

    w6, c6 = collections.deque(), collections.Counter()
    w24, c24 = collections.deque(), collections.Counter()
    eqs = ctx.equips
    i, n = 0, len(eqs)
    root_of, sinks_of = {}, {}

    for sl in ctx.slots:
        end = (sl + 1) * SLOT
        while i < n and eqs[i][0] < end:
            ts, t, v, bd, tx = eqs[i]
            if not ctx.is_internal(t):
                c6[t] += 1
                c24[t] += 1
                w6.append((ts, t))
                w24.append((ts, t))
            i += 1
        while w6 and w6[0][0] < end - T["conc_window_h"] * H:
            ts, t = w6.popleft()
            c6[t] -= 1
        while w24 and w24[0][0] < end - DAY:
            ts, t = w24.popleft()
            c24[t] -= 1

        # Re-cluster on a fixed slot lattice, never "N slots since the last one":
        # --as-of truncates the ledger, and a cadence counted from the start of
        # whatever range was loaded would put run.py and replay.py on different
        # lattices and break the parity gate.
        if sl % refresh == 0:
            lo = bisect.bisect_left(edge_ts, end - edge_win)
            hi = bisect.bisect_left(edge_ts, end)
            live = sorted(t for t, k in c24.items() if k > 0)
            root_of, sinks_of = cluster_map(shared_sinks(edges[lo:hi], live))

        nn = sum(k for k in c6.values() if k > 0)
        if nn >= n_min:
            per_cluster = collections.Counter()
            for t, k in c6.items():
                if k > 0 and t in root_of:
                    per_cluster[root_of[t]] += k
            for root, ck in per_cluster.items():
                members = sorted((t, c6[t]) for t in c6 if c6[t] > 0 and root_of.get(t) == root)
                if len(members) < 2:
                    continue
                mk = max(x[1] for x in members)
                if fires_share(ck, nn, mk, n_min, top1):
                    sinks = sinks_of[root]
                    fires.append(_finding(
                        "6h", cluster_id(sinks), round(ck / nn, 3), members, sinks, sl * SLOT,
                        [f"{len(members)} creator wallets paying into the same collecting wallet took "
                         f"{ck:,} of {nn:,} equip-sized payouts between them / 6 h",
                         f"largest single member took {mk:,}",
                         f"{len(sinks)} shared collecting wallet(s)"],
                        f"cluster6h {ck}/{nn} m={len(members)} top={mk} s={len(sinks)} "
                        f"sinks={_named(sinks)}"))

        per24 = collections.Counter()
        for t, k in c24.items():
            if k > 0 and t in root_of:
                per24[root_of[t]] += k
        for root, ck in per24.items():
            members = sorted((t, c24[t]) for t in c24 if c24[t] > 0 and root_of.get(t) == root)
            if len(members) < 2:
                continue
            mk = max(x[1] for x in members)
            if fires_aggregate(ck, mk, agg_min, sa_floor):
                sinks = sinks_of[root]
                fires.append(_finding(
                    "24h", cluster_id(sinks), ck, members, sinks, sl * SLOT,
                    [f"{len(members)} creator wallets paying into the same collecting wallet took "
                     f"{ck:,} equip-sized payouts between them / 24 h",
                     f"largest single member took {mk:,}",
                     f"{len(sinks)} shared collecting wallet(s)"],
                    f"cluster24h {ck} m={len(members)} top={mk} s={len(sinks)} "
                    f"sinks={_named(sinks)}"))
    return fires
