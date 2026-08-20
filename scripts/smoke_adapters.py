"""Exercise the FalkorDB and Kuzu adapters against a small slice of real data.

Neither had ever executed when this was written -- both were written against
their documentation. The Bolt adapter, which had been exercised, still needed
three fixes once real data went through it, so the prior on untested adapter
code is not good.

Uses the full prepared graph. An earlier version sliced it to a few thousand
rows for speed, which turned out to be a false economy: the slice was too
sparse to satisfy the degree and jurisdiction-frequency floors the parameter
pools require, so it produced empty pools and, before that, zero-row results
that read as adapter bugs. Both engines here are local and load the whole
graph in seconds, so there was nothing to save.

Timings printed here are a correctness check and are never published.

Usage:  .venv/bin/python scripts/smoke_adapters.py
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbench.adapters.falkor import FalkorAdapter  # noqa: E402
from gbench.adapters.kuzu_embedded import KuzuAdapter  # noqa: E402
from gbench.workloads.definitions import build_pools, build_registry  # noqa: E402
from gbench.workloads.registry import Dialect  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "data" / "build"
DOCKER = "/opt/homebrew/bin/docker"
CONTAINER = "gbench-adapter-smoke"
PORT = 6399
NODE_LIMIT = 4000




def exercise(adapter, label: str) -> bool:
    """Full lifecycle against the slice. Returns True if everything worked."""
    registry = build_registry(build_pools(BUILD))
    print(f"\n=== {label} ===")
    try:
        adapter.connect()
        print("  connect            ok")

        adapter.create_schema(registry.indexes)
        print("  create_schema      ok")

        result = adapter.load(str(BUILD / "nodes.csv"), str(BUILD / "rels.csv"), 1000)
        print(
            f"  load               ok  {result.nodes_loaded:,} nodes / "
            f"{result.relationships_loaded:,} rels in {result.wall_clock_s:.1f}s"
        )

        footprint = adapter.footprint()
        print(
            f"  footprint          ok  {footprint.node_count:,} nodes / "
            f"{footprint.relationship_count:,} rels"
        )

        failures = 0
        dialect = Dialect(adapter.dialect)
        rng = random.Random(1)
        for workload in registry.workloads:
            variant = workload.for_dialect(dialect)
            params = workload.params(rng) if workload.params else {}
            try:
                outcome = adapter.execute(variant.query, params)
                print(f"  {workload.id:<18} ok  {outcome.client_ms:6.2f}ms  {outcome.rows} rows")
            except Exception as exc:  # noqa: BLE001 - this script exists to find these
                failures += 1
                print(f"  {workload.id:<18} FAIL {type(exc).__name__}: {str(exc)[:90]}")

        adapter.close()
        return failures == 0
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED at lifecycle: {type(exc).__name__}: {str(exc)[:200]}")
        return False


def main() -> int:
    if not (BUILD / "nodes.csv").exists():
        print("run `make data` first")
        return 1
    ok = True

    # ── Kuzu: embedded, nothing to start ──
    kuzu_path = ROOT / "data" / "build" / "kuzu-smoke"
    if kuzu_path.exists():
        shutil.rmtree(kuzu_path)
    ok &= exercise(KuzuAdapter(name="kuzu", path=str(kuzu_path)), "Kuzu (embedded)")

    # ── FalkorDB: needs a container ──
    subprocess.run([DOCKER, "rm", "-f", CONTAINER], capture_output=True, check=False)
    subprocess.run(
        [DOCKER, "run", "-d", "--name", CONTAINER, "-p", f"{PORT}:6379",
         "falkordb/falkordb:latest"],
        capture_output=True, check=False,
    )  # fmt: skip
    time.sleep(6)
    ok &= exercise(
        FalkorAdapter(name="falkordb", host="localhost", port=PORT), "FalkorDB (RESP)"
    )
    subprocess.run([DOCKER, "rm", "-f", CONTAINER], capture_output=True, check=False)

    print("\n" + ("all adapters healthy" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
