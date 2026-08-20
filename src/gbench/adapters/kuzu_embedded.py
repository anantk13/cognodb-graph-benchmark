"""Kuzu adapter -- embedded, and reported in its own table for that reason.

Kuzu has no server. It runs inside the benchmark process, so there is no
protocol, no connection pool, no network, and nothing corresponding to
`result_available_after`. Placed in a row beside four client-server engines it
would win every latency comparison by virtue of not being one, which is why the
configuration marks it `report_separately` and the README gives it a table of
its own with that stated at the top.

It earns its place anyway. It is the only columnar, vectorised engine in the
study and the only one with a declared schema, so it answers a question the
others cannot: how much of a graph engine's latency is the graph, and how much
is being a server at all.

The declared schema is why `rels.csv` carries endpoint labels. Kuzu relationship
tables name the node tables they connect, so the twelve
(start label, type, end label) combinations present in this dataset have to be
known before any data is loaded -- and they are derived from the data rather
than hard-coded, so the schema cannot drift from what is actually there.
"""

from __future__ import annotations

import csv
import shutil
import time
from pathlib import Path
from typing import Any

import kuzu

from gbench.adapters.base import Adapter, Footprint, LoadResult, QueryResult

#: Columns carried on every node table, matching the prepared CSV.
NODE_COLUMNS = (
    "name",
    "jurisdiction",
    "countries",
    "country_codes",
    "status",
    "incorporation_date",
)


class KuzuAdapter(Adapter):
    """Kuzu, running in-process against a local database directory."""

    def __init__(self, name: str, path: str, dialect: str = "cypher_kuzu") -> None:
        self.name = name
        self.dialect = dialect
        self._path = Path(path)
        self._db: kuzu.Database | None = None
        self._conn: kuzu.Connection | None = None
        self._indexes: list[tuple[str, str]] = []

    # ── lifecycle ──────────────────────────────────────────────────────────

    def connect(self) -> None:
        # Started from empty every run. A benchmark that reuses a database
        # directory measures whatever the previous run left behind.
        if self._path.exists():
            shutil.rmtree(self._path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = kuzu.Database(str(self._path))
        self._conn = kuzu.Connection(self._db)
        self._conn.execute("RETURN 1")

    def close(self) -> None:
        self._conn = None
        self._db = None

    @property
    def conn(self) -> kuzu.Connection:
        if self._conn is None:
            raise RuntimeError(f"{self.name}: connect() has not been called")
        return self._conn

    # ── schema ─────────────────────────────────────────────────────────────

    def create_schema(self, indexes: list[tuple[str, str]]) -> None:
        """Record the index intent. Kuzu's tables are declared at load time.

        Kuzu is a declared-schema engine: node and relationship tables have to
        exist before any row can be inserted, and both are derived from the
        data itself in `load`. Deriving them here instead was the first attempt
        and it was wrong -- the label set was taken from `indexes`, so
        `Intermediary`, which is present in the graph but is not an indexed
        label, never got a table and the load failed with
        "Table Intermediary does not exist".

        `indexes` is otherwise advisory here. Kuzu indexes a node table's
        primary key automatically and offers no secondary index for
        `Entity.jurisdiction`, so the filtered lookup is an indexed seek on the
        other four engines and a scan on this one. That asymmetry is a real
        property of the engine and is stated in the results rather than papered
        over.
        """
        self._indexes = list(indexes)

    def _declare_nodes(self, nodes_csv: str) -> None:
        """Create one node table per label actually present in the data."""
        labels: set[str] = set()
        with open(nodes_csv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                labels.add(row["label"])

        columns = ", ".join(f"{c} STRING" for c in NODE_COLUMNS)
        for label in sorted(labels):
            self.conn.execute(
                # `bench_seq` is declared here although no row carries it at
                # load time. Kuzu is schema-first: SET on an undeclared
                # property fails with "Cannot find property", where the four
                # schema-free engines simply create it. Declaring it is the
                # minimum needed for the write workload to run at all, and the
                # asymmetry is recorded in the results rather than hidden.
                f"CREATE NODE TABLE IF NOT EXISTS {label}"
                f"(node_id STRING, {columns}, bench_seq INT64, PRIMARY KEY(node_id))"
            )

    def declare_relationships(self, combinations: list[tuple[str, str, str]]) -> None:
        """Create one relationship table per type, spanning its endpoint pairs.

        Derived from the data by `_relationship_combinations` so the schema
        matches the graph exactly. A pair present in the CSV but absent from
        the schema would silently drop those edges, leaving this target holding
        a smaller graph than every other one.
        """
        by_type: dict[str, list[tuple[str, str]]] = {}
        for start, rel_type, end in combinations:
            by_type.setdefault(rel_type, []).append((start, end))

        for rel_type, pairs in by_type.items():
            unique = sorted(set(pairs))
            spec = ", ".join(f"FROM {s} TO {e}" for s, e in unique)

            # Kuzu 0.7.1 rejects several FROM/TO pairs in one CREATE REL TABLE;
            # the construct for that is CREATE REL TABLE GROUP. Verified on this
            # version: a group is traversable by its own name, so
            # `-[:REGISTERED_ADDRESS]->` resolves across all of its pairs and
            # the query text stays byte-identical to the other four dialects.
            # Without the group, each pair would need its own table with a
            # compound name, and Kuzu's queries would have to be rewritten.
            keyword = "REL TABLE GROUP" if len(unique) > 1 else "REL TABLE"
            self.conn.execute(f"CREATE {keyword} IF NOT EXISTS {rel_type}({spec})")

    # ── measurement ────────────────────────────────────────────────────────

    def execute(self, query: str, params: dict[str, Any]) -> QueryResult:
        started = time.perf_counter()
        result = self.conn.execute(query, parameters=params or {})
        rows = 0
        while result.has_next():
            result.get_next()
            rows += 1
        client_ms = (time.perf_counter() - started) * 1000.0

        # No server, so no server-reported time. Recorded as None rather than
        # copied from the client figure: claiming a zero-network measurement
        # here would make the network-versus-engine split look like it applied
        # to a target where the question is meaningless.
        return QueryResult(client_ms=client_ms, server_ms=None, rows=rows)

    # ── ingest ─────────────────────────────────────────────────────────────

    def load(self, nodes_csv: str, rels_csv: str, batch_size: int) -> LoadResult:
        """Driver batching, at the same batch size as every other target.

        Kuzu's `COPY FROM` reads a CSV directly and is far faster. Not used,
        for the same reason Neo4j's offline importer and FalkorDB's bulk loader
        are not: the managed targets have no equivalent, so an ingest column
        mixing four mechanisms would compare nothing.
        """
        self._declare_nodes(nodes_csv)
        self.declare_relationships(_relationship_combinations(rels_csv))
        started = time.perf_counter()

        nodes_loaded = 0
        for label, batch in _batched_nodes(nodes_csv, batch_size):
            props = ", ".join(f"{c}: row.{c}" for c in ("node_id", *NODE_COLUMNS))
            self.conn.execute(
                f"UNWIND $rows AS row CREATE (n:{label} {{{props}}})",
                parameters={"rows": batch},
            )
            nodes_loaded += len(batch)

        rels_loaded = 0
        for (start_label, rel_type, end_label), batch in _batched_rels(rels_csv, batch_size):
            self.conn.execute(
                f"UNWIND $rows AS row "
                f"MATCH (a:{start_label} {{node_id: row.s}}), (b:{end_label} {{node_id: row.e}}) "
                f"CREATE (a)-[:{rel_type}]->(b)",
                parameters={"rows": batch},
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
        """On-disk size is directly observable here, unlike on any managed tier."""
        stored = None
        if self._path.exists():
            stored = sum(f.stat().st_size for f in self._path.rglob("*") if f.is_file())

        nodes = self.execute("MATCH (n) RETURN count(n) AS c", {})
        rels = self.execute("MATCH ()-[r]->() RETURN count(r) AS c", {})
        return Footprint(
            stored_bytes=stored,
            memory_bytes=None,
            node_count=nodes.rows and self._scalar("MATCH (n) RETURN count(n)"),
            relationship_count=rels.rows and self._scalar("MATCH ()-[r]->() RETURN count(r)"),
            notes={"dialect": self.dialect, "embedded": True, "path": str(self._path)},
        )

    def _scalar(self, query: str) -> int | None:
        result = self.conn.execute(query)
        return int(result.get_next()[0]) if result.has_next() else None

    def warmup_query(self) -> tuple[str, dict[str, Any]]:
        return "RETURN 1 AS n", {}


def _relationship_combinations(path: str) -> list[tuple[str, str, str]]:
    """Every (start label, type, end label) triple present in the file."""
    seen: set[tuple[str, str, str]] = set()
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            seen.add((row["start_label"], row["rel_type"], row["end_label"]))
    return sorted(seen)


def _batched_nodes(path: str, batch_size: int):
    """Batches keyed by label. Blanks are kept as empty strings rather than
    dropped, because Kuzu's declared schema requires every column present."""
    buffers: dict[str, list[dict[str, str]]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            label = row["label"]
            buffer = buffers.setdefault(label, [])
            buffer.append({k: v for k, v in row.items() if k != "label"})
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
