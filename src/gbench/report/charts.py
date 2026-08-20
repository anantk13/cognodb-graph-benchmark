"""Charts for the README, rendered from the same raw results as the tables.

Design rules applied throughout, each of them a correction to something charts
in this genre routinely get wrong:

*   **One y-axis, always.** Never two scales on one plot. Where two measures of
    different magnitude need showing together -- throughput and latency, say --
    they get two figures, not two axes.
*   **Colour follows the engine, never its rank.** `SERIES` maps a target id to
    a fixed hue, so a chart with one target missing does not repaint the rest.
*   **Direct labels on every bar.** Three of the six palette hues sit below 3:1
    against the surface, so identity is never carried by colour alone.
*   **Log scale where the range demands it, announced in the axis label.** A
    managed instance at 240 ms and a container at 1 ms on one linear axis makes
    the container invisible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# Categorical slots 1-6 of a palette validated for adjacent-pair CVD
# separation and normal-vision separation on a light surface.
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


def _style(ax: Any, *, xlabel: str = "", ylabel: str = "", title: str = "") -> None:
    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    if xlabel:
        ax.set_xlabel(xlabel, color=INK_MUTED, fontsize=10)
    if ylabel:
        ax.set_ylabel(ylabel, color=INK_MUTED, fontsize=10)
    if title:
        ax.set_title(title, color=INK, fontsize=13, fontweight="600", loc="left", pad=14)


def _colour(target_id: str) -> str:
    return SERIES.get(target_id, FALLBACK)


def latency_by_workload(
    rows: list[tuple[str, str, dict[str, float]]], out: Path, *, title: str
) -> Path:
    """Grouped bars: one group per workload, one bar per target.

    `rows` is (target label, target id, {workload_id: p50_ms}).
    """
    if not rows:
        return out
    workloads = list(next(iter(rows))[2].keys())
    n = len(rows)
    width = 0.8 / max(n, 1)

    fig, ax = plt.subplots(figsize=(11, 5))
    for i, (label, target_id, values) in enumerate(rows):
        xs = [j + i * width - 0.4 + width / 2 for j in range(len(workloads))]
        ys = [values.get(w, 0.0) for w in workloads]
        bars = ax.bar(
            xs, ys, width * 0.86, label=label, color=_colour(target_id), zorder=3
        )
        # Direct labels: three palette hues fall below 3:1 on this surface, so
        # identity and value must not depend on colour alone.
        for bar, y in zip(bars, ys, strict=True):
            if y > 0:
                ax.annotate(
                    f"{y:.1f}",
                    (bar.get_x() + bar.get_width() / 2, y),
                    ha="center",
                    va="bottom",
                    fontsize=7,
                    color=INK_MUTED,
                    rotation=90,
                    xytext=(0, 2),
                    textcoords="offset points",
                )

    ax.set_yscale("log")
    ax.set_xticks(range(len(workloads)))
    ax.set_xticklabels(workloads, rotation=20, ha="right")
    _style(ax, ylabel="p50 latency, ms (log scale)", title=title)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_MUTED, ncols=min(len(rows), 3))
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


def memory_sweep(series: dict[str, list[tuple[str, float | None]]], out: Path) -> Path:
    """Latency against the memory cap -- the degradation curve.

    A DNF is a gap in the line, not a zero. Plotting a failure as zero would
    make the engine that could not start look like the fastest one.
    """
    fig, ax = plt.subplots(figsize=(8, 5))
    tiers: list[str] = []
    for target_id, points in series.items():
        tiers = [tier for tier, _ in points]
        xs = [i for i, (_, value) in enumerate(points) if value is not None]
        ys = [value for _, value in points if value is not None]
        ax.plot(
            xs, ys, marker="o", markersize=8, linewidth=2,
            color=_colour(target_id), label=target_id, zorder=3,
        )  # fmt: skip
        for i, (_, value) in enumerate(points):
            if value is None:
                ax.annotate(
                    "DNF",
                    (i, ax.get_ylim()[0]),
                    ha="center",
                    va="bottom",
                    fontsize=8,
                    color=_colour(target_id),
                    fontweight="600",
                )

    ax.set_xticks(range(len(tiers)))
    ax.set_xticklabels(tiers)
    _style(
        ax,
        xlabel="container memory limit",
        ylabel="p50 latency, ms",
        title="Latency against memory cap (3-hop traversal)",
    )
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_MUTED)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


def concurrency(series: dict[str, list[tuple[int, float]]], out: Path) -> Path:
    """Sustained throughput against client concurrency."""
    fig, ax = plt.subplots(figsize=(8, 5))
    for target_id, points in series.items():
        xs = [c for c, _ in points]
        ys = [q for _, q in points]
        ax.plot(
            xs, ys, marker="o", markersize=8, linewidth=2,
            color=_colour(target_id), label=target_id, zorder=3,
        )  # fmt: skip
        if points:
            ax.annotate(
                f"{points[-1][1]:,.0f}",
                points[-1],
                fontsize=9,
                color=_colour(target_id),
                xytext=(6, 0),
                textcoords="offset points",
                va="center",
                fontweight="600",
            )
    _style(
        ax,
        xlabel="concurrent clients",
        ylabel="sustained queries/second",
        title="Mixed workload throughput (90% reads)",
    )
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_MUTED)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


def network_split(rows: list[tuple[str, float, float]], out: Path) -> Path:
    """How much of a managed target's latency was ever the database.

    Stacked: server-reported execution time, then everything else. A 2px
    surface gap separates the segments so the boundary is readable without
    relying on the two fills contrasting.
    """
    fig, ax = plt.subplots(figsize=(8, 3.4))
    labels = [label for label, _, _ in rows]
    server = [s for _, s, _ in rows]
    network = [n for _, _, n in rows]
    ys = range(len(rows))

    ax.barh(ys, server, color="#2a78d6", label="server execution", zorder=3, height=0.55)
    ax.barh(
        ys, network, left=server, color="#c9c8c2", label="network + driver",
        zorder=3, height=0.55, edgecolor=SURFACE, linewidth=2,
    )  # fmt: skip

    for i, (s, n) in enumerate(zip(server, network, strict=True)):
        ax.annotate(
            f"{s:.0f} ms server  ·  {n:.0f} ms network  ({100*n/(s+n):.0f}% network)",
            (s + n, i),
            xytext=(8, 0),
            textcoords="offset points",
            va="center",
            fontsize=9,
            color=INK_MUTED,
        )

    ax.set_yticks(list(ys))
    ax.set_yticklabels(labels)
    ax.set_xlim(0, max(s + n for s, n in zip(server, network, strict=True)) * 1.7)
    _style(ax, xlabel="milliseconds", title="Where the time actually goes (point lookup)")
    ax.grid(axis="y", visible=False)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_MUTED, loc="lower right")
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


def warmup_curve(series: dict[str, list[float]], out: Path, *, window: int = 10) -> Path:
    """Latency against iteration during warm-up.

    Published rather than assumed: only a minority of measured benchmark pairs
    ever reach a steady state, and some get slower over time. A reader should
    be able to see whether this target had settled before measurement began.
    """
    fig, ax = plt.subplots(figsize=(9, 4.5))
    for target_id, samples in series.items():
        if not samples:
            continue
        smoothed = [
            sum(samples[max(0, i - window) : i + 1]) / len(samples[max(0, i - window) : i + 1])
            for i in range(len(samples))
        ]
        ax.plot(
            range(len(smoothed)), smoothed, linewidth=2,
            color=_colour(target_id), label=target_id, zorder=3,
        )  # fmt: skip
    ax.set_yscale("log")
    _style(
        ax,
        xlabel="warm-up iteration",
        ylabel=f"latency, ms (log scale, {window}-iteration mean)",
        title="Warm-up curve — did the engine reach a steady state?",
    )
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_MUTED, ncols=2)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out
