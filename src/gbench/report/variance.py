"""Run-to-run variance across repeated executions of the whole suite.

A single run measures variation *within* itself -- 1,000 iterations per
workload give a confidence interval on each percentile. It says nothing about
variation *between* runs, which has different and usually larger sources:
free tiers are multi-tenant and a neighbour's load is invisible from outside,
containers start with different cache states, a burstable CPU allocation may
have more or less credit available, and the host machine's thermal and
scheduling state differs.

Without this, a difference between two engines cannot be distinguished from
noise. The concrete case in this study: Neo4j's aggregation measured 2.00 ms
of server time against FalkorDB's 2.68 ms in a single run, and "Neo4j wins the
aggregation" was written on that basis. A 34% gap from one sample, at a
protocol whose server-time resolution is 1 ms, is exactly the claim that needs
a spread before it can be published.

The output is deliberately blunt. Where the spread across runs is wide enough
to cover a difference between engines, the table says so rather than leaving a
reader to compare medians and assume the gap is real.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Coefficient of variation above which a measurement is called unstable.
#: Not a standard; chosen so that a reader is warned before comparing two
#: engines whose gap is smaller than either one's own run-to-run spread.
UNSTABLE_CV = 0.10


@dataclass(frozen=True)
class Spread:
    """One measurement observed across several runs."""

    target: str
    metric: str
    values: list[float]

    @property
    def runs(self) -> int:
        return len(self.values)

    @property
    def median(self) -> float:
        return statistics.median(self.values)

    @property
    def minimum(self) -> float:
        return min(self.values)

    @property
    def maximum(self) -> float:
        return max(self.values)

    @property
    def spread_pct(self) -> float:
        """Range as a percentage of the median. Zero for a single run."""
        return 100.0 * (self.maximum - self.minimum) / self.median if self.median else 0.0

    @property
    def cv(self) -> float:
        """Coefficient of variation. Undefined below two runs, reported as 0."""
        if self.runs < 2 or not self.median:
            return 0.0
        return statistics.stdev(self.values) / statistics.mean(self.values)

    @property
    def stable(self) -> bool:
        return self.runs >= 2 and self.cv <= UNSTABLE_CV


def _run_dirs(raw_dir: Path) -> list[Path]:
    return sorted(p for p in raw_dir.iterdir() if p.is_dir())


def collect(raw_dir: Path) -> dict[tuple[str, str], Spread]:
    """Gather every comparable measurement from every run directory.

    Keyed by (target label, metric). A target that did not finish in some run
    contributes nothing for that run rather than a zero, so a DNF never drags a
    median down as though it were a fast result.
    """
    gathered: dict[tuple[str, str], list[float]] = {}

    for run in _run_dirs(raw_dir):
        for path in sorted(run.rglob("*.json")):
            data = json.loads(path.read_text())
            if data.get("error") or (data.get("container") or {}).get("dnf"):
                continue

            target = (data.get("target") or {}).get("id", path.stem)
            label = f"{target} @ {data['tier']}" if data.get("tier") else target

            load = data.get("load") or {}
            if load.get("relationships_per_second"):
                gathered.setdefault((label, "ingest rels/s"), []).append(
                    load["relationships_per_second"]
                )

            for entry in data.get("workloads") or []:
                client = entry.get("client")
                if client and client.get("p50_ms") is not None:
                    key = (label, f"{entry['workload_id']} p50 ms")
                    gathered.setdefault(key, []).append(client["p50_ms"])

            for level in data.get("concurrency") or []:
                key = (label, f"{level['clients']}-client q/s")
                gathered.setdefault(key, []).append(level["qps"])

    return {k: Spread(k[0], k[1], v) for k, v in gathered.items()}


def table(spreads: Iterable[Spread], *, metric_filter: str | None = None) -> str:
    """Markdown table of medians with their run-to-run spread."""
    rows = [
        s for s in spreads if s.runs >= 2 and (metric_filter is None or metric_filter in s.metric)
    ]
    if not rows:
        return "_Only one run recorded; run-to-run variance cannot be reported._"

    rows.sort(key=lambda s: (s.target, s.metric))
    lines = [
        "| Target | Metric | Runs | Median | Min | Max | Spread | CV |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for s in rows:
        flag = "" if s.stable else " ⚠"
        lines.append(
            f"| `{s.target}` | {s.metric} | {s.runs} | {s.median:,.2f} | {s.minimum:,.2f} "
            f"| {s.maximum:,.2f} | {s.spread_pct:,.1f}% | {s.cv:.3f}{flag} |"
        )
    return "\n".join(lines)


def unstable(spreads: Iterable[Spread]) -> list[Spread]:
    """Measurements whose run-to-run spread makes them unsafe to compare."""
    return sorted(
        (s for s in spreads if s.runs >= 2 and not s.stable),
        key=lambda s: -s.cv,
    )


def summarise(raw_dir: Path) -> dict[str, Any]:
    """Everything the report needs about run-to-run variance."""
    spreads = collect(raw_dir)
    values = list(spreads.values())
    repeated = [s for s in values if s.runs >= 2]
    return {
        "run_count": len(_run_dirs(raw_dir)),
        "measurements": len(values),
        "repeated": len(repeated),
        "unstable": unstable(values),
        "median_cv": statistics.median([s.cv for s in repeated]) if repeated else 0.0,
        "spreads": values,
    }
