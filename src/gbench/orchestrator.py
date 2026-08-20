"""Runs an arm end to end and writes raw results.

Every target in an arm receives the identical call sequence in the identical
order: connect, index, load, measure the round-trip floor, run every workload,
run the concurrency sweep, record the footprint, disconnect. The orchestrator
cannot tell the targets apart -- everything peculiar to one of them lives
behind the adapter interface -- which is what makes "the same thing was done to
each" a property of the code rather than a claim in a README.

Nothing is aggregated here. Every iteration's latency is written out
individually, so percentiles can be recomputed, the warm-up curve can be
plotted, and a reader can check the arithmetic instead of trusting it.
"""

from __future__ import annotations

import json
import platform
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gbench import config as cfg
from gbench.adapters.base import Adapter
from gbench.adapters.bolt import BoltAdapter
from gbench.adapters.falkor import FalkorAdapter
from gbench.adapters.kuzu_embedded import KuzuAdapter
from gbench.runner import containers
from gbench.runner.concurrency import ConcurrencyConfig, run_sweep
from gbench.runner.latency import RunConfig, measure_round_trip_floor, run_workload
from gbench.workloads.definitions import build_pools, build_registry

HOST_PORT = 7699
LOCAL_PASSWORD = "benchmarkpassword"


def build_adapter(target: cfg.Target, *, host_port: int = HOST_PORT) -> Adapter:
    """Construct the adapter for a target, without connecting it."""
    creds = target.credentials
    if target.adapter == "bolt":
        uri = creds.get("uri", "")
        if target.arm == "capped":
            # The container publishes on a host port the orchestrator chose,
            # so the configured URI is overridden rather than duplicated in
            # config for every tier.
            uri = f"bolt://localhost:{host_port}"
        return BoltAdapter(
            name=target.id,
            uri=uri,
            user=creds.get("user", "") or "neo4j",
            password=creds.get("password", "") or LOCAL_PASSWORD,
            dialect=target.dialect,
            supports_auth=target.id != "memgraph",
        )
    if target.adapter == "falkordb":
        return FalkorAdapter(
            name=target.id,
            host=creds.get("host", "localhost"),
            port=int(creds.get("port", host_port)),
            dialect=target.dialect,
        )
    if target.adapter == "kuzu":
        return KuzuAdapter(name=target.id, path=creds.get("path", "data/build/kuzu"))
    raise ValueError(f"unknown adapter {target.adapter!r} for target {target.id!r}")


def environment() -> dict[str, Any]:
    """Where this ran. Published so the numbers can be placed in context.

    One published benchmark ran on a 2009 server and another on a shared CI
    runner, in both cases without saying so. Disclosing the host up front costs
    nothing and is the difference between a result and an anecdote.
    """
    return {
        "recorded_at": datetime.now(UTC).isoformat(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def measure_target(
    adapter: Adapter,
    target: cfg.Target,
    config: cfg.Config,
    *,
    build_dir: Path,
    quick: bool = False,
) -> dict[str, Any]:
    """Run the full measurement sequence against one connected target."""
    pools = build_pools(build_dir)
    registry = build_registry(pools)

    run_cfg = (
        RunConfig(warmup_iterations=20, measure_iterations=120)
        if quick
        else RunConfig(
            warmup_iterations=int(config.run.get("warmup_iterations", 200)),
            measure_iterations=int(config.run.get("measure_iterations", 1000)),
            seed=int(config.run.get("seed", 20260820)),
        )
    )

    record: dict[str, Any] = {
        "target": target.redacted(),
        "environment": environment(),
        "run_config": {
            "warmup_iterations": run_cfg.warmup_iterations,
            "measure_iterations": run_cfg.measure_iterations,
            "seed": run_cfg.seed,
            "indexes": [list(pair) for pair in registry.indexes],
        },
    }

    floor = measure_round_trip_floor(adapter, iterations=100 if not quick else 50)
    record["round_trip_floor"] = floor.as_dict() if floor else None

    adapter.create_schema(registry.indexes)

    batch = int(config.run.get("load_batch_size", 1000))
    load = adapter.load(str(build_dir / "nodes.csv"), str(build_dir / "rels.csv"), batch)
    record["load"] = {
        "wall_clock_s": load.wall_clock_s,
        "nodes_loaded": load.nodes_loaded,
        "relationships_loaded": load.relationships_loaded,
        "nodes_per_second": load.nodes_per_second,
        "relationships_per_second": load.relationships_per_second,
        "batch_size": load.batch_size,
        "method": load.method,
    }

    record["workloads"] = [
        run_workload(adapter, workload, run_cfg).as_dict() for workload in registry.workloads
    ]

    if not quick:
        levels = tuple(config.run.get("concurrency_levels", (1, 10, 40)))
        sweep = run_sweep(
            adapter,
            registry.read_workloads(),
            registry.get("write_tag"),
            ConcurrencyConfig(
                levels=levels,
                read_write_ratio=float(config.run.get("mixed_read_write_ratio", 0.9)),
                seed=run_cfg.seed,
            ),
        )
        record["concurrency"] = [level.as_dict() for level in sweep]

    footprint = adapter.footprint()
    record["footprint"] = {
        "stored_bytes": footprint.stored_bytes,
        "memory_bytes": footprint.memory_bytes,
        "node_count": footprint.node_count,
        "relationship_count": footprint.relationship_count,
        "notes": footprint.notes,
    }

    # The graph every target holds must be the graph the manifest describes.
    # A target that loaded 380,000 of 381,523 relationships is not running the
    # same benchmark, and the discrepancy would otherwise be invisible.
    manifest = json.loads((build_dir / "manifest.json").read_text())
    record["graph_matches_manifest"] = (
        footprint.node_count == manifest["total_nodes"]
        and footprint.relationship_count == manifest["total_relationships"]
    )
    return record


def run_capped_arm(
    config: cfg.Config, out_dir: Path, *, build_dir: Path, quick: bool = False
) -> list[dict[str, Any]]:
    """Sweep every containerised engine across every memory tier."""
    records: list[dict[str, Any]] = []

    for tier in config.tiers:
        for target in config.by_arm("capped"):
            if target.image is None:
                continue  # embedded targets are handled separately

            command = containers.build_command(
                target_id=target.id,
                image=target.image,
                tier_id=tier.id,
                memory=tier.memory,
                cpus=tier.cpus,
                port=target.port or 7687,
                host_port=HOST_PORT,
                password=LOCAL_PASSWORD,
            )
            print(f"  {target.id} @ {tier.id}: starting")
            container = containers.start(command, target.id, tier.id, target.image)
            container = containers.wait_ready(
                container, containers.probe_command(target.id, LOCAL_PASSWORD)
            )

            record: dict[str, Any] = {"container": container.as_dict(), "tier": tier.id}
            if not container.ready:
                # A DNF is a published result, not a gap. It is exactly what
                # "this engine cannot run in CognoDB's free-tier envelope"
                # looks like when measured rather than assumed.
                print(
                    f"  {target.id} @ {tier.id}: DNF "
                    f"(exit={container.exit_code} oom_killed={container.oom_killed})"
                )
                record["target"] = target.redacted()
                record["dnf_reason"] = (
                    "out of memory" if container.oom_killed else "did not become ready"
                )
                records.append(record)
                _write(out_dir / "capped" / f"{target.id}@{tier.id}.json", record)
                containers.stop(target.id, tier.id)
                continue

            adapter = build_adapter(target)
            try:
                with adapter:
                    record |= measure_target(
                        adapter, target, config, build_dir=build_dir, quick=quick
                    )
                print(f"  {target.id} @ {tier.id}: done")
            except Exception as exc:  # noqa: BLE001 - a failed target is reported
                record["error"] = f"{type(exc).__name__}: {exc}"[:1000]
                print(f"  {target.id} @ {tier.id}: FAILED {exc}")
            finally:
                containers.stop(target.id, tier.id)

            records.append(record)
            _write(out_dir / "capped" / f"{target.id}@{tier.id}.json", record)

    return records


def run_managed_arm(
    config: cfg.Config, out_dir: Path, *, build_dir: Path, quick: bool = False
) -> list[dict[str, Any]]:
    """Measure the managed free tiers as shipped."""
    records: list[dict[str, Any]] = []
    for target in config.by_arm("managed"):
        print(f"  {target.id}: connecting")
        adapter = build_adapter(target)
        record: dict[str, Any] = {}
        try:
            with adapter:
                record = measure_target(adapter, target, config, build_dir=build_dir, quick=quick)
            print(f"  {target.id}: done")
        except Exception as exc:  # noqa: BLE001 - a failed target is reported
            record = {"target": target.redacted(), "error": f"{type(exc).__name__}: {exc}"[:1000]}
            print(f"  {target.id}: FAILED {exc}")
        records.append(record)
        _write(out_dir / "managed" / f"{target.id}.json", record)
    return records


def _write(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, default=str) + "\n")


def new_run_dir(root: Path) -> Path:
    """A directory named for when the run started, so runs never overwrite."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    path = root / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path
