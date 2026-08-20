"""Mixed read/write workload at increasing client concurrency.

The assignment asks for "sustained queries/second with a stated client
concurrency and read/write mix". Throughput at a fixed number of clients is a
closed-loop measurement by definition -- N workers, each issuing its next
request when its previous one returns -- and that is what `run_sweep` does.

The consequence has to be stated rather than hidden. A closed-loop client
under-samples stalls: when the server freezes for a second, N workers each
record one slow request instead of the hundreds that should have been issued
during the freeze. Throughput is unaffected by this (the work genuinely did not
happen), but latency percentiles are, and increasingly so towards the tail --
roughly 1x distortion at p50, 1.5x at p95, and 20x or worse at p99.

So this module publishes throughput as its headline, p50 and p95 alongside it,
and no p99 at all. `open_loop_latency` is provided for the honest version of
the latency question: it issues requests on a fixed schedule regardless of
whether earlier ones have returned, so a stall shows up as every request that
should have been sent during it.

Connection pools are sized above the highest concurrency level on every target.
At 40 clients against a pool of 16, the measurement is of workers queueing for
connections, which is a property of the harness rather than of the database.
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from gbench.adapters.base import Adapter
from gbench.report.stats import Summary, summarise
from gbench.workloads.registry import Dialect, Workload


@dataclass(frozen=True)
class ConcurrencyConfig:
    """Parameters for the sweep, held identical across every target."""

    levels: tuple[int, ...] = (1, 10, 40)
    duration_s: float = 30.0
    warmup_s: float = 5.0
    read_write_ratio: float = 0.9
    """Fraction of operations that are reads. 0.9 means 90% reads, 10% writes."""

    seed: int = 20260820


@dataclass
class LevelResult:
    """One concurrency level against one target."""

    clients: int
    duration_s: float
    reads: int = 0
    writes: int = 0
    failures: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    error_samples: list[str] = field(default_factory=list)

    @property
    def completed(self) -> int:
        return self.reads + self.writes

    @property
    def qps(self) -> float:
        """Sustained operations per second -- the headline number.

        Unaffected by coordinated omission: work that did not happen during a
        stall is correctly absent from the count.
        """
        return self.completed / self.duration_s if self.duration_s else 0.0

    @property
    def achieved_read_ratio(self) -> float | None:
        return self.reads / self.completed if self.completed else None

    def summary(self) -> Summary | None:
        return summarise(self.latencies_ms) if self.latencies_ms else None

    def as_dict(self) -> dict[str, Any]:
        s = self.summary()
        return {
            "clients": self.clients,
            "duration_s": self.duration_s,
            "completed": self.completed,
            "reads": self.reads,
            "writes": self.writes,
            "failures": self.failures,
            "qps": self.qps,
            "achieved_read_ratio": self.achieved_read_ratio,
            "closed_loop": True,
            "latency": s.as_dict() if s else None,
            "error_samples": self.error_samples[:10],
        }


def run_sweep(
    adapter: Adapter,
    read_workloads: list[Workload],
    write_workload: Workload,
    config: ConcurrencyConfig,
) -> list[LevelResult]:
    """Measure sustained throughput at each concurrency level."""
    dialect = Dialect(adapter.dialect)
    results: list[LevelResult] = []

    for clients in config.levels:
        _drive(adapter, read_workloads, write_workload, dialect, clients, config.warmup_s, config)
        results.append(
            _drive(
                adapter,
                read_workloads,
                write_workload,
                dialect,
                clients,
                config.duration_s,
                config,
                record=True,
            )
        )
    return results


def _drive(
    adapter: Adapter,
    read_workloads: list[Workload],
    write_workload: Workload,
    dialect: Dialect,
    clients: int,
    duration_s: float,
    config: ConcurrencyConfig,
    *,
    record: bool = False,
) -> LevelResult:
    """Run `clients` workers for `duration_s`, optionally recording results."""
    result = LevelResult(clients=clients, duration_s=duration_s)
    lock = threading.Lock()
    deadline = time.perf_counter() + duration_s

    def worker(worker_id: int) -> None:
        # Each worker gets its own seeded generator, offset by worker id, so a
        # run is reproducible while workers do not all hammer the same node.
        rng = random.Random(config.seed + worker_id)
        while time.perf_counter() < deadline:
            is_read = rng.random() < config.read_write_ratio
            workload = rng.choice(read_workloads) if is_read else write_workload
            variant = workload.for_dialect(dialect)
            params = workload.params(rng) if workload.params else {}

            started = time.perf_counter()
            try:
                adapter.execute(variant.query, params)
            except Exception as exc:  # noqa: BLE001 - failures under load are data
                if record:
                    with lock:
                        result.failures += 1
                        if len(result.error_samples) < 10:
                            result.error_samples.append(f"{type(exc).__name__}: {exc}"[:200])
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000.0

            if record:
                with lock:
                    result.latencies_ms.append(elapsed_ms)
                    if is_read:
                        result.reads += 1
                    else:
                        result.writes += 1

    with ThreadPoolExecutor(max_workers=clients) as pool:
        for future in [pool.submit(worker, i) for i in range(clients)]:
            future.result()

    return result


def open_loop_latency(
    adapter: Adapter,
    workload: Workload,
    target_qps: float,
    duration_s: float = 30.0,
    seed: int = 20260820,
) -> Summary | None:
    """Latency at a fixed arrival rate, immune to coordinated omission.

    Requests are scheduled at `1/target_qps` intervals and each one's latency is
    measured from the instant it *should* have been sent, not from when a worker
    got around to sending it. A one-second stall therefore appears as every
    request that should have gone out during that second, which is what a user
    behind the stall actually experienced.

    Run at a fraction of the throughput `run_sweep` measured, since driving an
    open loop at or above capacity produces an unbounded backlog and measures
    the backlog.
    """
    dialect = Dialect(adapter.dialect)
    variant = workload.for_dialect(dialect)
    rng = random.Random(seed)

    interval = 1.0 / target_qps
    started = time.perf_counter()
    scheduled = started
    samples: list[float] = []
    lock = threading.Lock()

    def issue(due_at: float, params: dict[str, Any]) -> None:
        try:
            adapter.execute(variant.query, params)
        except Exception:  # noqa: BLE001, S110 - failures excluded from latency
            return
        # Measured from the scheduled time, not from the actual send.
        with lock:
            samples.append((time.perf_counter() - due_at) * 1000.0)

    with ThreadPoolExecutor(max_workers=64) as pool:
        while time.perf_counter() - started < duration_s:
            scheduled += interval
            now = time.perf_counter()
            if scheduled > now:
                time.sleep(scheduled - now)
            params = workload.params(rng) if workload.params else {}
            pool.submit(issue, scheduled, params)

    return summarise(samples) if samples else None
