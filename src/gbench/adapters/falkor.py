"""FalkorDB adapter, over RESP rather than Bolt.

FalkorDB does expose a Bolt port, and using it would have let this target share
the Bolt adapter and its single code path -- which is exactly the kind of
uniformity this harness prizes. It is not used, because FalkorDB's own
documentation calls its Bolt support "experimental and not recommended for
production". Benchmarking an engine through a code path its authors tell you
not to use measures the experiment, not the engine.

So this target gets its own client, and the cost of that is stated plainly in
the README: FalkorDB's client-side overhead is not identical to the four Bolt
targets', and a few milliseconds of the difference between them is the client
library rather than the database. The server-side time reported below is not
subject to that, which is why it is the number the comparison leans on.

FalkorDB is also the only engine in this study that survived the 512 MB tier,
where both the JVM engine and Memgraph's default image were killed by the
kernel. That result is what makes including it worthwhile.
"""

from __future__ import annotations

import csv
import time
from typing import Any

from falkordb import FalkorDB

from gbench.adapters.base import Adapter, Footprint, LoadResult, QueryResult

GRAPH_KEY = "bench"


class FalkorAdapter(Adapter):
    """FalkorDB, driven with the native client over the Redis protocol."""

    def __init__(self, name: str, host: str, port: int, dialect: str = "opencypher9") -> None:
        self.name = name
        self.dialect = dialect
        self._host = host
        self._port = int(port)
        self._db: FalkorDB | None = None
        self._graph: Any | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def connect(self) -> None:
        self._db = FalkorDB(host=self._host, port=self._port)
        self._graph = self._db.select_graph(GRAPH_KEY)
        # Liveness check, matching the Bolt adapter's behaviour so that neither
        # target enters measurement warmer than the other.
        self._graph.query("RETURN 1")

    def close(self) -> None:
        self._graph = None
        self._db = None

    @property
    def graph(self) -> Any:
        if self._graph is None:
            raise RuntimeError(f"{self.name}: connect() has not been called")
        return self._graph

    # ── reset ──────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Drop the whole graph key. Cheaper than deleting node by node."""
        try:
            self.graph.delete()
        except Exception:  # noqa: BLE001 - absent graph is already clear
            pass
        self._graph = self._db.select_graph(GRAPH_KEY)  # type: ignore[union-attr]

    # ── schema ─────────────────────────────────────────────────────────────

    def create_schema(self, indexes: list[tuple[str, str]]) -> None:
        """Create the identical (label, property) index set as every other target."""
        for label, prop in indexes:
            try:
                self.graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception as exc:  # noqa: BLE001 - only "already exists" is tolerable
                if "already indexed" not in str(exc).lower() and "exist" not in str(exc).lower():
                    raise RuntimeError(
                        f"{self.name}: failed to create index on {label}.{prop}: {exc}"
                    ) from exc

    # ── measurement ────────────────────────────────────────────────────────

    def execute(self, query: str, params: dict[str, Any]) -> QueryResult:
        started = time.perf_counter()
        result = self.graph.query(query, params or None)
        client_ms = (time.perf_counter() - started) * 1000.0

        # FalkorDB reports its own execution time, so this target supports the
        # same network-versus-server split as the Bolt targets do.
        server_ms = getattr(result, "run_time_ms", None)
        rows = len(result.result_set) if result.result_set is not None else 0
        return QueryResult(
            client_ms=client_ms,
            server_ms=float(server_ms) if server_ms is not None else None,
            rows=rows,
        )

    # ── ingest ─────────────────────────────────────────────────────────────

    def load(self, nodes_csv: str, rels_csv: str, batch_size: int) -> LoadResult:
        """Driver batching at the same batch size as every other target.

        FalkorDB ships a dedicated `falkordb-bulk-insert` CSV loader that is
        considerably faster. It is not used here for the same reason Neo4j's
        offline importer is not: the managed targets have no equivalent, and an
        ingest column that mixes four mechanisms compares nothing. The bulk
        loader is listed in the README as a faster path measured-but-not-used.
        """
        started = time.perf_counter()

        nodes_loaded = 0
        for label, batch in _batched_nodes(nodes_csv, batch_size):
            self.graph.query(
                f"UNWIND $rows AS row CREATE (n:{label}) SET n = row", {"rows": batch}
            )
            nodes_loaded += len(batch)

        rels_loaded = 0
        for (start_label, rel_type, end_label), batch in _batched_rels(rels_csv, batch_size):
            self.graph.query(
                f"UNWIND $rows AS row "
                f"MATCH (a:{start_label} {{node_id: row.s}}) "
                f"MATCH (b:{end_label} {{node_id: row.e}}) "
                f"CREATE (a)-[:{rel_type}]->(b)",
                {"rows": batch},
            )
            rels_loaded += len(batch)

        return LoadResult(
            wall_clock_s=time.perf_counter() - started,
            nodes_loaded=nodes_loaded,
            relationships_loaded=rels_loaded,
            batch_size=batch_size,
            method="driver batching, UNWIND + CREATE, one statement per label/type group",
        )

    # ── observation ────────────────────────────────────────────────────────

    def footprint(self) -> Footprint:
        nodes = self.graph.query("MATCH (n) RETURN count(n)")
        rels = self.graph.query("MATCH ()-[r]->() RETURN count(r)")

        # FalkorDB runs on Redis, which reports its own memory use -- so unlike
        # every managed target here, this one can actually answer the
        # "resource usage where observable" question with a number.
        memory_bytes: int | None = None
        try:
            info = self._db.connection.info("memory")  # type: ignore[union-attr]
            memory_bytes = int(info.get("used_memory", 0)) or None
        except Exception:  # noqa: BLE001 - absent metric is recorded as absent
            memory_bytes = None

        return Footprint(
            node_count=nodes.result_set[0][0] if nodes.result_set else None,
            relationship_count=rels.result_set[0][0] if rels.result_set else None,
            memory_bytes=memory_bytes,
            stored_bytes=None,
            notes={"dialect": self.dialect, "protocol": "RESP", "graph_key": GRAPH_KEY},
        )

    def warmup_query(self) -> tuple[str, dict[str, Any]]:
        return "RETURN 1", {}


# Batching mirrors the Bolt adapter's helpers exactly. Duplicated deliberately
# rather than shared through an import, because the two clients want different
# parameter shapes and a shared helper with a mode flag would be the kind of
# per-target branch this harness is built to avoid.


def _batched_nodes(path: str, batch_size: int):
    buffers: dict[str, list[dict[str, str]]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            label = row["label"]
            buffer = buffers.setdefault(label, [])
            buffer.append({k: v for k, v in row.items() if k != "label" and v != ""})
            if len(buffer) >= batch_size:
                yield label, buffer
                buffers[label] = []
    for label, buffer in buffers.items():
        if buffer:
            yield label, buffer


def _batched_rels(path: str, batch_size: int):
    buffers: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["start_label"], row["rel_type"], row["end_label"])
            buffer = buffers.setdefault(key, [])
            buffer.append({"s": row["start_id"], "e": row["end_id"]})
            if len(buffer) >= batch_size:
                yield key, buffer
                buffers[key] = []
    for key, buffer in buffers.items():
        if buffer:
            yield key, buffer
