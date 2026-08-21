"""Charts for the report, rendered from the same raw results as the tables.

Design rules, each a correction to a defect found by rendering an earlier
version and inspecting the output:

*   **One series per engine, never per engine-and-tier.** An earlier version
    plotted all three memory tiers of each engine, which produced three
    indistinguishable bars of the same colour side by side: colour encoded the
    engine while the legend labelled engine-plus-tier. Latency is flat across
    tiers, so the tier comparison belongs in its own chart.
*   **The two arms never share an axis.** A container on loopback and a managed
    instance 240 ms away are not comparable on client latency, and one plot
    invites exactly the comparison the round-trip floor exists to prevent.
*   **The legend sits outside the plot area.** Inside, it covered the data.
*   **Labels are selective.** Fifty-six numbers on fifty-six bars is noise; the
    exact figures appear in the tables above each chart.
*   **One y-axis**, never two scales on one plot.
*   **A DNF is a labelled gap, never a zero**, which would render an engine
    that could not start as the fastest one present.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# Categorical slots from a palette validated for adjacent-pair colour-vision
# separation and normal-vision separation against a light surface. Colour
# follows the engine and never its rank, so a chart missing one target does not
# repaint the others.
SERIES: dict[str, str] = {
    "cognodb-c0": "#2a78d6",
    "neo4j-aura-free": "#eb6834",
    "neo4j-community": "#1baf7a",
    "memgraph": "#eda100",
    "falkordb": "#e87ba4",
    "kuzu": "#008300",
}
FALLBACK = "#52514e"

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_MUTED = "#52514e"
GRID = "#e4e3df"

DISPLAY = {
    "cognodb-c0": "CognoDB c0",
    "neo4j-aura-free": "Neo4j AuraDB Free",
    "neo4j-community": "Neo4j Community",
    "memgraph": "Memgraph",
    "falkordb": "FalkorDB",
    "kuzu": "Kuzu",
}

WORKLOAD_SHORT = {
    "point_lookup": "Point\nlookup",
    "filtered_lookup": "Filtered\nlookup",
    "hop1": "1-hop",
    "hop2": "2-hop",
    "hop3": "3-hop",
    "aggregation": "Aggregation",
    "write_tag": "Write",
}


def _style(ax: Any, *, xlabel: str = "", ylabel: str = "", title: str = "") -> None:
    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, labelsize=10)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    if xlabel:
        ax.set_xlabel(xlabel, color=INK_MUTED, fontsize=11, labelpad=10)
    if ylabel:
        ax.set_ylabel(ylabel, color=INK_MUTED, fontsize=11, labelpad=10)
    if title:
        ax.set_title(title, color=INK, fontsize=14, fontweight="600", loc="left", pad=18)


def _colour(engine: str) -> str:
    return SERIES.get(engine, FALLBACK)


def _label(engine: str) -> str:
    return DISPLAY.get(engine, engine)


def _legend_below(ax: Any, columns: int) -> None:
    """Legend beneath the axis, clear of every mark."""
    ax.legend(
        frameon=False,
        fontsize=10,
        labelcolor=INK_MUTED,
        ncols=max(columns, 1),
        loc="upper center",
        bbox_to_anchor=(0.5, -0.17),
        handlelength=1.6,
        columnspacing=2.2,
    )


def latency_by_workload(
    rows: list[tuple[str, dict[str, float]]], out: Path, *, title: str, subtitle: str = ""
) -> Path:
    """Grouped bars: one group per workload, one bar per engine.

    `rows` is (engine id, {workload_id: p50_ms}), reduced by the caller to one
    entry per engine.
    """
    if not rows:
        return out

    workloads = [w for w in WORKLOAD_SHORT if any(w in values for _, values in rows)]
    count = len(rows)
    width = 0.78 / max(count, 1)

    fig, ax = plt.subplots(figsize=(12, 5.6))
    for index, (engine, values) in enumerate(rows):
        offsets = [i + index * width - 0.39 + width / 2 for i in range(len(workloads))]
        heights = [values.get(w) for w in workloads]
        ax.bar(
            [x for x, h in zip(offsets, heights, strict=True) if h],
            [h for h in heights if h],
            width * 0.88,
            label=_label(engine),
            color=_colour(engine),
            zorder=3,
        )

    ax.set_yscale("log")
    ax.set_xticks(range(len(workloads)))
    ax.set_xticklabels([WORKLOAD_SHORT[w] for w in workloads], fontsize=10)
    ax.set_xlim(-0.6, len(workloads) - 0.4)
    _style(ax, ylabel="p50 latency, ms — log scale", title=title)
    if subtitle:
        # The title is lifted to make room; drawn at the default pad the two
        # overlapped each other.
        ax.title.set_position((0, 1.0))
        ax.set_title(title, color=INK, fontsize=14, fontweight="600", loc="left", pad=44)
        ax.text(
            0, 1.012, subtitle, transform=ax.transAxes,
            fontsize=10.5, color=INK_MUTED, va="bottom", linespacing=1.5,
        )  # fmt: skip
    _legend_below(ax, min(count, 5))
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out


def memory_sweep(series: dict[str, list[tuple[str, float | None]]], out: Path) -> Path:
    """Three-hop latency against the container memory limit."""
    fig, ax = plt.subplots(figsize=(9, 5.2))
    tiers: list[str] = []
    for engine, points in series.items():
        tiers = [tier for tier, _ in points]
        xs = [i for i, (_, value) in enumerate(points) if value is not None]
        ys = [value for _, value in points if value is not None]
        ax.plot(
            xs, ys, marker="o", markersize=9, linewidth=2.4,
            color=_colour(engine), label=_label(engine), zorder=3,
        )  # fmt: skip

    # Placed after every line is drawn, in axes coordinates, so a marker's
    # position does not depend on the y-limit as it stood mid-loop.
    for engine, points in series.items():
        for i, (_, value) in enumerate(points):
            if value is None:
                ax.annotate(
                    "DNF\nout of memory",
                    (i, 0.04),
                    xycoords=ax.get_xaxis_transform(),
                    ha="center", va="bottom", fontsize=10,
                    color=_colour(engine), fontweight="700", linespacing=1.4,
                )  # fmt: skip

    ax.set_xticks(range(len(tiers)))
    ax.set_xticklabels(tiers, fontsize=11)
    ax.set_xlim(-0.35, len(tiers) - 0.65)
    _style(
        ax,
        xlabel="container memory limit — CPU held at 0.5 throughout",
        ylabel="3-hop traversal, p50 latency in ms",
        title="Latency is flat across memory tiers; only startup depends on memory",
    )
    _legend_below(ax, 3)
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out


def concurrency(series: dict[str, list[tuple[int, float]]], out: Path, *, title: str) -> Path:
    """Sustained throughput against client concurrency. One arm per chart."""
    if not series:
        return out
    fig, ax = plt.subplots(figsize=(9.5, 5.2))
    ordered = sorted(series.items(), key=lambda kv: -(kv[1][-1][1] if kv[1] else 0))

    for engine, points in ordered:
        ax.plot(
            [c for c, _ in points], [q for _, q in points],
            marker="o", markersize=8, linewidth=2.4,
            color=_colour(engine), label=_label(engine), zorder=3,
        )  # fmt: skip

    # One label per line at its right end, nudged apart so they never overlap.
    ends = sorted(((p[-1][1], e) for e, p in ordered if p), reverse=True)
    values = [v for v, _ in ends]
    span = (max(values) - min(values)) or max(values) or 1.0
    previous: float | None = None
    for value, engine in ends:
        y = value
        if previous is not None and previous - y < span * 0.07:
            y = previous - span * 0.07
        ax.annotate(
            f"{value:,.0f}",
            (max(c for c, _ in series[engine]), y),
            xytext=(10, 0), textcoords="offset points", va="center",
            fontsize=10.5, color=_colour(engine), fontweight="700",
        )  # fmt: skip
        previous = y

    ax.set_xticks([1, 10, 40])
    ax.set_xlim(-2, 49)
    _style(ax, xlabel="concurrent clients", ylabel="sustained queries per second", title=title)
    _legend_below(ax, min(len(ordered), 3))
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out


def network_split(rows: list[tuple[str, float, float]], out: Path) -> Path:
    """How much of a managed target's latency was ever the database."""
    if not rows:
        return out
    labels = [_label(name) for name, _, _ in rows]
    server = [s for _, s, _ in rows]
    network = [n for _, _, n in rows]
    ys = range(len(rows))
    widest = max(s + n for s, n in zip(server, network, strict=True))

    fig, ax = plt.subplots(figsize=(11, 2.2 + 0.6 * len(rows)))
    ax.barh(ys, server, color="#2a78d6", label="server execution", zorder=3, height=0.4)
    ax.barh(
        ys, network, left=server, color="#c9c8c2", label="network + driver",
        zorder=3, height=0.4, edgecolor=SURFACE, linewidth=2,
    )  # fmt: skip

    for i, (s, n) in enumerate(zip(server, network, strict=True)):
        ax.annotate(
            f"{s:.0f} ms server  ·  {n:.0f} ms network  ·  {100 * n / (s + n):.0f}% network",
            (s + n, i), xytext=(12, 0), textcoords="offset points",
            va="center", fontsize=10.5, color=INK_MUTED,
        )  # fmt: skip

    ax.set_yticks(list(ys))
    ax.set_yticklabels(labels, fontsize=11)
    ax.set_xlim(0, widest * 2.2)
    ax.set_ylim(-0.6, len(rows) - 0.4)
    _style(
        ax,
        xlabel="milliseconds",
        title="Point lookup: almost none of the latency is the database",
    )
    ax.grid(axis="y", visible=False)
    _legend_below(ax, 2)
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out


def warmup_curve(series: dict[str, list[float]], out: Path, *, window: int = 15) -> Path:
    """Latency against iteration during warm-up, published rather than assumed.

    Only a minority of measured benchmark pairs reach a steady state and some
    grow slower over time, so a reader must be able to see whether a target had
    settled before measurement began.
    """
    if not series:
        return out
    fig, ax = plt.subplots(figsize=(10, 4.8))
    for engine, samples in series.items():
        if not samples:
            continue
        smoothed = [
            sum(samples[max(0, i - window) : i + 1]) / len(samples[max(0, i - window) : i + 1])
            for i in range(len(samples))
        ]
        ax.plot(
            range(len(smoothed)), smoothed, linewidth=2.2,
            color=_colour(engine), label=_label(engine), zorder=3,
        )  # fmt: skip

    ax.set_yscale("log")
    _style(
        ax,
        xlabel="warm-up iteration — discarded from the published percentiles",
        ylabel=f"3-hop latency, ms — log scale, {window}-iteration mean",
        title="Warm-up curves: did each engine reach a steady state?",
    )
    _legend_below(ax, 3)
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out
