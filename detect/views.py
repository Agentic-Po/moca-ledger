"""Matplotlib renderers — one PNG per finding, written into its incident folder.

Charts per the spec: concentration bars with the organic p50/p95 band, burst
timeline, fan-in tree, watchlist/context table; generic value-vs-threshold bar
for everything else. Neutral wording; entity keys truncated to 14 chars.
"""
import collections
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from signals import SLOT, H, DAY, TREASURY, utc

FIG = dict(figsize=(8, 4.5), dpi=110)


def _style(ax, title):
    ax.set_title(title, fontsize=10)
    ax.tick_params(labelsize=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def render(finding, ctx, path):
    try:
        fn = {
            "10": _concentration, "10n": _concentration, "10i": _concentration,
            "11": _burst,
            "4b": _fanin, "S-C": _fanin, "4a": _rate, "15": _rate, "EV": _rate,
            "S-Q": _rate, "S-Q2": _rate,
            "S-G": _table, "S-F": _table, "S-A": _burst,
        }.get(finding.signal, _generic)
        fig = fn(finding, ctx)
        fig.savefig(path, bbox_inches="tight")
        plt.close(fig)
        return True
    except Exception:
        plt.close("all")
        return False


def _concentration(f, ctx):
    """Two variants must render differently:
       - share variant  (window '6h')     : share of equip-sized payouts per creator
       - count variant  (window '60min')  : payout COUNT per creator in the hour
       Rendering a count finding on a share axis makes the flagged bar look
       below threshold — never do that."""
    end = f.ts + SLOT
    count_variant = str(getattr(f, "window", "")).startswith("60")
    span = 1 * H if count_variant else 6 * H
    counts = collections.Counter()
    for ts, t, v, bd, tx in ctx.equips:
        if end - span <= ts < end and not ctx.is_internal(t):
            counts[t] += 1
    total = sum(counts.values()) or 1
    top = counts.most_common(8)
    fig, ax = plt.subplots(**FIG)
    labels = [w[:12] for w, _ in top]
    colors = ["#c0392b" if w == f.key else "#5b7fa6" for w, _ in top]
    if count_variant:
        ys = [k for _, k in top]
        ax.bar(range(len(top)), ys, color=colors)
        try: ax.axhline(float(f.threshold), color="#c0392b", ls="--", lw=1, label=f"page threshold ({f.threshold}/60 min)")
        except (TypeError, ValueError): pass
        ax.set_ylabel("equip-sized payouts / 60 min")
        title = f"payout rate per recipient · hour ending {utc(end)} UTC · n={total}"
    else:
        ys = [k / total for _, k in top]
        ax.bar(range(len(top)), ys, color=colors)
        org = ctx.baselines.get("daily_creator_rewards_organic", {}).get("top1_share_moca", {})
        if org:
            ax.axhspan(org.get("p50", 0), org.get("p95", 0), color="#9acd9a", alpha=0.3, label="organic p50-p95")
        thr = f.threshold if isinstance(f.threshold, (int, float)) and 0 < float(f.threshold) <= 1 else 0.45
        ax.axhline(float(thr), color="#c0392b", ls="--", lw=1, label=f"page threshold ({thr:.2f})")
        ax.set_ylabel("share of equip-sized payouts / 6 h")
        title = f"concentration · window ending {utc(end)} UTC · n={total}"
    ax.set_xticks(range(len(top)))
    ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.legend(fontsize=8)
    _style(ax, title)
    return fig


def _burst(f, ctx):
    end = f.ts + SLOT
    pts = [(ts, v) for ts, t, v, bd, tx in ctx.equips if t == f.key and end - 24 * H <= ts < end]
    fig, ax = plt.subplots(**FIG)
    if pts:
        xs = [(ts - end) / H for ts, v in pts]
        ax.plot(xs, range(1, len(pts) + 1), drawstyle="steps-post", color="#5b7fa6")
        ax.scatter(xs, range(1, len(pts) + 1), s=8, color="#c0392b")
    ax.set_xlabel("hours before window end")
    ax.set_ylabel("cumulative equip-sized payouts")
    _style(ax, f"payout timeline · {f.key[:12]} · ending {utc(end)} UTC")
    return fig


def _fanin(f, ctx):
    end = f.ts + SLOT
    inflows = collections.Counter()
    for ts, ff, tt, v, tx, b in ctx.rows:
        if tt == f.key and end - DAY <= ts < end and ff != TREASURY:
            inflows[ff] += v
    top = inflows.most_common(12)
    fig, ax = plt.subplots(**FIG)
    n = len(top)
    for i, (w, v) in enumerate(top):
        y = 1 - (i + 0.5) / max(n, 1)
        ax.plot([0.05, 0.7], [y, 0.5], color="#5b7fa6", lw=max(0.5, min(5, v / 500)))
        ax.text(0.04, y, f"{w[:12]} ({v:,.0f})", ha="right", va="center", fontsize=7)
    ax.text(0.72, 0.5, f"{f.key[:12]}\nsink", ha="left", va="center", fontsize=9,
            bbox=dict(boxstyle="round", fc="#f6d0d0"))
    ax.set_xlim(-0.35, 1.0)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title(f"fan-in · {len(inflows)} senders / 24 h · ending {utc(end)} UTC", fontsize=10)
    return fig


def _rate(f, ctx):
    fig, ax = plt.subplots(**FIG)
    sl_end = f.ts // SLOT
    xs, ys = [], []
    per_slot = collections.Counter()
    if f.signal in ("15",):
        for ts, t, v, bd, tx in ctx.pay:
            per_slot[ts // SLOT] += 1
        win = 6
    else:
        for ts, t, v, bd, tx in ctx.equips:
            per_slot[ts // SLOT] += 1
        win = 144
    run = collections.deque()
    tot = 0
    for sl in range(sl_end - 288, sl_end + 1):
        run.append(sl)
        tot += per_slot.get(sl, 0)
        while run and run[0] <= sl - win:
            tot -= per_slot.get(run.popleft(), 0)
        xs.append((sl - sl_end) / 6)
        ys.append(tot)
    ax.plot(xs, ys, color="#5b7fa6")
    if isinstance(f.threshold, (int, float)):
        ax.axhline(f.threshold, color="#c0392b", ls="--", lw=1, label="threshold")
        ax.legend(fontsize=8)
    ax.set_xlabel("hours before now")
    ax.set_ylabel("rolling count")
    _style(ax, f"{f.signal} rolling rate · ending {utc(f.ts + SLOT)} UTC")
    return fig


def _table(f, ctx):
    fig, ax = plt.subplots(**FIG)
    ax.axis("off")
    rows = [[str(c)[:22] for c in r] for r in (f.evidence or [])][:14]
    if rows:
        tb = ax.table(cellText=rows, loc="center")
        tb.auto_set_font_size(False)
        tb.set_fontsize(7)
        tb.scale(1, 1.2)
    else:
        ax.text(0.5, 0.5, "\n".join(f.headline) or f.detail, ha="center", va="center", fontsize=9)
    ax.set_title(f"{f.signal} · {f.key[:12]} · {utc(f.ts + SLOT)} UTC", fontsize=10)
    return fig


def _generic(f, ctx):
    fig, ax = plt.subplots(**FIG)
    v = f.value if isinstance(f.value, (int, float)) else 0
    t = f.threshold if isinstance(f.threshold, (int, float)) else 0
    ax.bar(["value", "threshold"], [v, t], color=["#c0392b", "#999999"])
    _style(ax, f"{f.signal} · {f.key[:12]} · {utc(f.ts + SLOT)} UTC")
    return fig
