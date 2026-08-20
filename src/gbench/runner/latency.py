"""Closed-loop latency measurement.

The loop is deliberately boring: warm up, then time N iterations, recording
every single one. Three things about it are not boring, and each exists to close
a documented way benchmarks go wrong.

**Warm-up is recorded, not discarded.** Everyone warms up; almost nobody shows
the curve. Barrett et al. (OOPSLA 2017) found that only 43.5% of measured
virtual-machine benchmark pairs ever reach a steady state at all, and about 18%
get *slower* over time -- so "we warmed up for 200 iterations" is an assumption,
not a fact, unless the curve is published. The warm-up samples are kept and
written to the results file so a reader can check whether the target had
actually settled before measurement began.

**Parameters come from a seeded generator shared across targets.** Every target
sees the identical sequence of start nodes. A published comparison drew all its
traversals from a single hard-coded start node, which measures one path through
one graph rather than the graph; drawing *different* random nodes per target
would be worse still, because then the targets are not answering the same
question.

**Row counts are collected and compared.** If a query returns 12 rows on one
engine and 0 on another, the two are not running the same query however similar
the text looks. That check has caught real dialect-translation errors.

This runner is closed-loop: it issues the next request only after the previous
one returns. That under-samples stalls -- a one-second freeze yields one slow
sample instead of the thousand requests that should have been issued during it.
The distortion is negligible at p50 and around 1.5x at p95, but 20x or worse at
p99, which is why p99 is not reported from this path at all. Sustained-throughput
numbers come from the open-loop generator in `concurrency.py` instead.
"""

from __future__ import annotations

import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from gbench.adapters.base import Adapter
from gbench.report.stats import Summary, minimum_samples_for, summarise
from gbench.workloads.registry import Dialect, Workload


@dataclass(frozen=True)
class RunConfig:
    """Measurement parameters, held identical across every target."""

    warmup_iterations: int = 200
    measure_iterations: int = 1000
    """1000, not the commonly suggested 100. A 95% confidence interval for p95
    is unbounded below n=110, so a p95 from 100 iterations carries no error bar.
    See `stats.minimum_samples_for`."""

    seed: int = 20260820
    """Fixed so a re-run reproduces the same start nodes exactly."""

    fail_fast_after: int = 25
    """Consecutive failures before abandoning this workload on this target. A
    target that cannot answer is a result worth reporting; a target that cannot
    answer a thousand times is a waste of the clock."""

    def __post_init__(self) -> None:
        floor = minimum_samples_for(0.95)
        if self.measure_iterations < floor:
            raise ValueError(
                f"measure_iterations={self.measure_iterations} cannot yield a bounded "
                f"95% CI for p95; need at least {floor}"
            )


@dataclass
class Failure:
    """One failed iteration, kept rather than swallowed."""

    iteration: int
    error_type: str
    message: str


@dataclass
class WorkloadResult:
    """Everything one workload produced against one target."""

    workload_id: str
    target: str
    dialect: str
    query: str
    rewrite_reason: str | None

    warmup_ms: list[float] = field(default_factory=list)
    """Kept so the warm-up curve can be plotted and inspected."""

    client_ms: list[float] = field(default_factory=list)
    server_ms: list[float] = field(default_factory=list)
    """Server-reported execution time, where the target reports it. Shorter than
    `client_ms` when some iterations did not report; never padded."""

    row_counts: Counter[int] = field(default_factory=Counter)
    failures: list[Failure] = field(default_factory=list)
    aborted: bool = False

    def summary(self) -> Summary | None:
        return summarise(self.client_ms) if self.client_ms else None

    def server_summary(self) -> Summary | None:
        return summarise(self.server_ms) if self.server_ms else None

    def overhead_summary(self) -> Summary | None:
        """Network plus driver cost, isolated.

        Only computable when the target reported server time for every
        measured iteration; a partial series would mix two different
        populations and the resulting percentile would mean nothing.
        """
        if not self.server_ms or len(self.server_ms) != len(self.client_ms):
            return None
        return summarise([c - s for c, s in zip(self.client_ms, self.server_ms, strict=True)])

    @property
    def modal_row_count(self) -> int | None:
        """The row count seen most often. Compared across targets."""
        return self.row_counts.most_common(1)[0][0] if self.row_counts else None

    @property
    def row_count_is_stable(self) -> bool:
        """False when a workload returned differing row counts across iterations.

        Expected for parameterised traversals, where different start nodes have
        different neighbourhoods. Recorded so the cross-target comparison knows
        whether an exact match is meaningful.
        """
        return len(self.row_counts) <= 1

    def as_dict(self) -> dict[str, Any]:
        summary = self.summary()
        server = self.server_summary()
        overhead = self.overhead_summary()
        return {
            "workload_id": self.workload_id,
            "target": self.target,
            "dialect": self.dialect,
            "query": self.query,
            "rewrite_reason": self.rewrite_reason,
            "iterations_measured": len(self.client_ms),
            "iterations_failed": len(self.failures),
            "aborted": self.aborted,
            "client": summary.as_dict() if summary else None,
            "server": server.as_dict() if server else None,
            "overhead": overhead.as_dict() if overhead else None,
            "row_counts": dict(self.row_counts),
            "row_count_stable": self.row_count_is_stable,
            "failures": [
                {"iteration": f.iteration, "type": f.error_type, "message": f.message}
                for f in self.failures[:20]
            ],
            "warmup_ms": self.warmup_ms,
            "client_ms": self.client_ms,
            "server_ms": self.server_ms,
        }


def run_workload(
    adapter: Adapter,
    workload: Workload,
    config: RunConfig,
) -> WorkloadResult:
    """Measure one workload against one target."""
    dialect = Dialect(adapter.dialect)
    variant = workload.for_dialect(dialect)

    result = WorkloadResult(
        workload_id=workload.id,
        target=adapter.name,
        dialect=adapter.dialect,
        query=variant.query,
        rewrite_reason=variant.rewrite_reason,
    )

    # Two independent generators seeded from the same base. Warm-up must not
    # consume draws that would otherwise have gone to the measured phase, or
    # changing the warm-up count would silently change which nodes get measured.
    warm_rng = random.Random(config.seed)
    measure_rng = random.Random(config.seed + 1)

    def draw(rng: random.Random) -> dict[str, Any]:
        return workload.params(rng) if workload.params else {}

    # ── warm-up: recorded, then excluded from the published percentiles ──
    for _ in range(config.warmup_iterations):
        try:
            outcome = adapter.execute(variant.query, draw(warm_rng))
            result.warmup_ms.append(outcome.client_ms)
        except Exception as exc:  # noqa: BLE001 - a warm-up failure is data
            result.failures.append(Failure(-1, type(exc).__name__, str(exc)[:400]))

    # ── measurement ──
    consecutive_failures = 0
    for i in range(config.measure_iterations):
        try:
            outcome = adapter.execute(variant.query, draw(measure_rng))
        except Exception as exc:  # noqa: BLE001 - failures are reported, not hidden
            result.failures.append(Failure(i, type(exc).__name__, str(exc)[:400]))
            consecutive_failures += 1
            if consecutive_failures >= config.fail_fast_after:
                result.aborted = True
                break
            continue

        consecutive_failures = 0
        result.client_ms.append(outcome.client_ms)
        result.row_counts[outcome.rows] += 1
        if outcome.server_ms is not None:
            result.server_ms.append(outcome.server_ms)

    return result


def measure_round_trip_floor(adapter: Adapter, iterations: int = 200) -> Summary | None:
    """Latency of a query that does no work, through the real driver and TLS.

    This is the floor beneath which no measured latency on this target can go:
    connection handling, TLS, protocol framing, and the physical distance to the
    instance. Reported alongside every result so that a reader comparing a
    managed instance in another datacentre against a container on loopback can
    see how much of the difference was ever attributable to the database.

    Warmed up first, for the same reason every other workload is: measured
    against a cold pool on a local container this returned a p50 of 2.2 ms and
    a p95 of 72 ms, and a "floor" with a 70 ms tail is describing connection
    setup and JIT rather than the round trip it is meant to bound.

    Presenting a floor measured this way as an established technique would be
    overclaiming -- it is this harness's method, cross-checked against the
    server-reported execution times that Bolt provides independently.
    """
    query, params = adapter.warmup_query()

    for _ in range(min(50, iterations)):
        try:
            adapter.execute(query, params)
        except Exception:  # noqa: BLE001, S110 - warm-up failures surface below
            pass

    samples: list[float] = []
    for _ in range(iterations):
        try:
            samples.append(adapter.execute(query, params).client_ms)
        except Exception:  # noqa: BLE001, S110 - a floor we cannot measure is None
            continue
    return summarise(samples) if samples else None


def timed(fn: Any) -> tuple[Any, float]:
    """Run `fn`, returning its value and its wall-clock duration in seconds."""
    started = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - started
