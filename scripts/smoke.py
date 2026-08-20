"""End-to-end validation against one local engine.

Proves the whole chain works -- container, adapter, index creation, ingest,
workloads, timing, percentiles -- before a single managed target is touched.
Runs a reduced iteration count; it is a correctness check, not a measurement,
and its numbers are never published.

Usage:  .venv/bin/python scripts/smoke.py [tier]      (default 2g)
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbench.adapters.bolt import BoltAdapter  # noqa: E402
from gbench.runner.latency import RunConfig, measure_round_trip_floor, run_workload  # noqa: E402
from gbench.workloads.definitions import build_pools, build_registry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "data" / "build"
CONTAINER = "gbench-smoke"
PORT = 7690
PASSWORD = "benchmarkpassword"

BUDGET_MB = {"512m": 280, "1g": 560, "2g": 1120}


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/opt/homebrew/bin/docker", *args], capture_output=True, text=True, check=False
    )


def start(tier: str) -> None:
    docker("rm", "-f", CONTAINER)
    budget = BUDGET_MB[tier]
    print(f"starting neo4j:5.26-community at {tier} (heap {budget*2//3}m, pagecache {budget//3}m)")
    docker(
        "run", "-d", "--name", CONTAINER,
        "--cpus=0.5", f"--memory={tier}", f"--memory-swap={tier}",
        "-e", f"NEO4J_AUTH=neo4j/{PASSWORD}",
        "-e", f"NEO4J_server_memory_heap_initial__size={budget*2//3}m",
        "-e", f"NEO4J_server_memory_heap_max__size={budget*2//3}m",
        "-e", f"NEO4J_server_memory_pagecache_size={budget//3}m",
        "-p", f"{PORT}:7687",
        "neo4j:5.26-community",
    )  # fmt: skip


def wait_ready(timeout_s: int = 180) -> bool:
    """Poll until the engine answers a query -- not until its log says it started.

    Neo4j prints `Started.` and is then killed by the kernel at tighter memory
    limits, so a log-based readiness check reports a dead container as healthy.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        probe = docker(
            "exec", CONTAINER, "cypher-shell", "-u", "neo4j", "-p", PASSWORD, "RETURN 1"
        )
        if probe.returncode == 0:
            return True
        state = docker("inspect", "-f", "{{.State.Status}}", CONTAINER).stdout.strip()
        if state != "running":
            oom = docker("inspect", "-f", "{{.State.OOMKilled}}", CONTAINER).stdout.strip()
            print(f"  container died (oom_killed={oom})")
            return False
        time.sleep(5)
    print("  timed out")
    return False


def enforced_limits() -> dict[str, str]:
    """Read the cap back from inside the container.

    Recording the requested limit proves nothing; recording what the kernel
    enforced proves the cap applied.
    """
    out = {}
    for f in ("cpu.max", "memory.max"):
        r = docker("exec", CONTAINER, "cat", f"/sys/fs/cgroup/{f}")
        out[f] = r.stdout.strip() if r.returncode == 0 else "unavailable"
    return out


def main() -> int:
    tier = sys.argv[1] if len(sys.argv) > 1 else "2g"
    start(tier)
    if not wait_ready():
        return 1
    print(f"  ready. enforced cgroup: {enforced_limits()}\n")

    adapter = BoltAdapter(
        name=f"neo4j-community@{tier}",
        uri=f"bolt://localhost:{PORT}",
        user="neo4j",
        password=PASSWORD,
        dialect="cypher5",
    )

    pools = build_pools(BUILD)
    registry = build_registry(pools)

    with adapter:
        floor = measure_round_trip_floor(adapter, iterations=50)
        print(
            f"round-trip floor (RETURN 1): "
            f"p50 {floor.p50.value:.2f}ms  p95 {floor.p95.value:.2f}ms"
        )

        print(f"\ncreating {len(registry.indexes)} indexes...")
        adapter.create_schema(registry.indexes)

        print("loading (driver batching, batch=1000)...")
        result = adapter.load(str(BUILD / "nodes.csv"), str(BUILD / "rels.csv"), 1000)
        print(
            f"  {result.nodes_loaded:,} nodes + {result.relationships_loaded:,} rels "
            f"in {result.wall_clock_s:.1f}s"
        )
        print(
            f"  {result.nodes_per_second:,.0f} nodes/s   "
            f"{result.relationships_per_second:,.0f} rels/s"
        )

        fp = adapter.footprint()
        print(f"  verified in-database: {fp.node_count:,} nodes / {fp.relationship_count:,} rels")

        cfg = RunConfig(warmup_iterations=20, measure_iterations=120)
        print(f"\nworkloads (warmup {cfg.warmup_iterations}, measure {cfg.measure_iterations}):")
        print(f"  {'workload':<18}{'p50':>9}{'p95':>9}{'server p50':>12}{'rows':>8}")
        for workload in registry.workloads:
            r = run_workload(adapter, workload, cfg)
            s, srv = r.summary(), r.server_summary()
            if s is None:
                why = r.failures[0].message[:60] if r.failures else ""
                print(f"  {workload.id:<18}FAILED  {why}")
                continue
            server = f"{srv.p50.value:.2f}ms" if srv else "n/a"
            print(
                f"  {workload.id:<18}{s.p50.value:>7.2f}ms{s.p95.value:>7.2f}ms"
                f"{server:>12}{r.modal_row_count:>8}"
            )

    docker("rm", "-f", CONTAINER)
    print("\nsmoke test complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
