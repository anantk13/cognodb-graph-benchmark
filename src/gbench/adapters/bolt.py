"""Bolt adapter -- CognoDB c0, Neo4j AuraDB Free, Neo4j Community, Memgraph.

Four targets, one class, one driver, one code path. That is the whole point:
anything that differs between these four is a constructor argument, never a
branch inside `execute`. A published comparison was retracted after it emerged
that the harness gave one engine a connection pool of 1 and every other engine
a pool of 25 -- a difference that lived in per-target setup code. Here the pool
size is a field on the base class and the same value reaches every target.

Server-side timing is the reason this adapter is worth writing carefully. Bolt
returns `result_available_after`, the milliseconds the server spent before the
first record was ready. Subtracting it from the wall-clock duration isolates
network and driver cost, which is what makes a managed instance in another
datacentre comparable with a container on loopback. Without it, a benchmark run
from a laptop over home broadband is substantially a measurement of the
broadband.
"""

from __future__ import annotations

import csv
import time
from collections.abc import Iterator
from typing import Any

from neo4j import Driver, GraphDatabase
from neo4j.exceptions import Neo4jError

from gbench.adapters.base import Adapter, Footprint, LoadResult, QueryResult


class BoltAdapter(Adapter):
    """A target reachable over the Bolt protocol with the official Neo4j driver."""

    def __init__(
        self,
        name: str,
        uri: str,
        user: str,
        password: str,
        dialect: str,
        *,
        database: str | None = None,
        supports_auth: bool = True,
    ) -> None:
        self.name = name
        self.dialect = dialect
        self._uri = uri
        self._user = user
        self._password = password
        self._database = database
        # Memgraph runs unauthenticated by default. Passing empty credentials
        # rather than omitting auth keeps the driver construction identical.
        self._supports_auth = supports_auth
        self._driver: Driver | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def connect(self) -> None:
        auth = (self._user, self._password) if self._supports_auth else None
        self._driver = GraphDatabase.driver(
            self._uri,
            auth=auth,
            # Identical across every Bolt target. See the module docstring.
            max_connection_pool_size=self.pool_size,
            connection_acquisition_timeout=60.0,
            max_transaction_retry_time=30.0,
        )
        # Liveness only. Deliberately not a warm-up -- warm-up is the runner's
        # job and has to be counted, not hidden inside connection setup.
        self._driver.verify_connectivity()

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    @property
    def driver(self) -> Driver:
        if self._driver is None:
            raise RuntimeError(f"{self.name}: connect() has not been called")
        return self._driver

    # ── schema ─────────────────────────────────────────────────────────────

    def create_schema(self, indexes: list[tuple[str, str]]) -> None:
        """Create the identical set of indexes on every target.

        Index syntax is one of the few places these four engines genuinely
        diverge, so the divergence is handled here and nowhere else. Whatever
        happens, the same (label, property) pairs are indexed everywhere -- an
        index present on one engine and missing on another produced one of the
        largest and least reproducible speedups in the published literature.
        """
        for label, prop in indexes:
            if self.dialect == "cypher_memgraph":
                stmt = f"CREATE INDEX ON :{label}({prop})"
            else:
                stmt = (
                    f"CREATE INDEX idx_{label.lower()}_{prop.lower()} "
                    f"IF NOT EXISTS FOR (n:{label}) ON (n.{prop})"
                )
            try:
                self._run(stmt, {})
            except Neo4jError as exc:
                # An index that already exists is fine; anything else is not,
                # because a silently missing index invalidates the comparison.
                if "already exists" not in str(exc).lower():
                    raise RuntimeError(
                        f"{self.name}: failed to create index on {label}.{prop}: {exc}"
                    ) from exc

    # ── measurement ────────────────────────────────────────────────────────

    def execute(self, query: str, params: dict[str, Any]) -> QueryResult:
        """Run one query, timing it from both ends of the wire.

        The clock starts before the driver call and stops after the result is
        fully consumed. Consuming matters: Bolt streams, so timing only up to
        the first record would measure how fast the server started answering
        rather than how long the answer took.
        """
        session = self.driver.session(database=self._database)
        try:
            started = time.perf_counter()
            result = session.run(query, params)
            records = result.data()
            summary = result.consume()
            client_ms = (time.perf_counter() - started) * 1000.0
        finally:
            session.close()

        # `result_available_after` is server-reported and in milliseconds. It is
        # None on engines that do not populate it, which is recorded honestly
        # rather than substituted with a guess.
        server_ms: float | None = None
        available = summary.result_available_after
        consumed = summary.result_consumed_after
        if available is not None:
            server_ms = float(available) + float(consumed or 0)

        return QueryResult(client_ms=client_ms, server_ms=server_ms, rows=len(records))

    def _run(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute without timing. For schema and bookkeeping only."""
        with self.driver.session(database=self._database) as session:
            return session.run(query, params).data()

    # ── ingest ─────────────────────────────────────────────────────────────

    def load(self, nodes_csv: str, rels_csv: str, batch_size: int) -> LoadResult:
        """Ingest by driver batching -- UNWIND over a parameter list.

        Deliberately not each engine's fastest path. CognoDB exposes no
        LOAD CSV at all and documents driver batching as the way in; Aura
        supports LOAD CSV from a remote URL but not neo4j-admin import; Neo4j
        Community supports an offline bulk importer that the managed targets
        cannot use. Timing three different mechanisms and printing them in one
        column would produce a number that means nothing. So every target gets
        the same mechanism at the same batch size, and the README lists the
        faster native path each engine offers as measured-but-not-used.
        """
        started = time.perf_counter()

        # Nodes are grouped by label and relationships by (start label, type,
        # end label), because neither a label nor a relationship type can be a
        # query parameter in Cypher. Grouping keeps the statement text constant
        # within a batch, so every engine gets the same number of distinct
        # statements to plan -- an engine re-planning on every batch while
        # another reuses a cached plan would be an ingest difference that has
        # nothing to do with ingest.
        nodes_loaded = 0
        for label, batch in _batched_nodes(nodes_csv, batch_size):
            self._run(
                f"UNWIND $rows AS row CREATE (n:{label}) SET n = row",
                {"rows": batch},
            )
            nodes_loaded += len(batch)

        rels_loaded = 0
        for (start_label, rel_type, end_label), batch in _batched_rels(rels_csv, batch_size):
            # Both endpoints are matched by label so the per-label index on
            # node_id is used. Without the label this degrades to a full scan
            # per row on every engine, and the measurement becomes a scan
            # benchmark wearing an ingest label.
            self._run(
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
        """Report what the target will disclose about itself.

        Managed free tiers disclose very little, and the honest response to that
        is to record None and render 'not observable' -- not to substitute an
        estimate. Counts are always available and are used to verify that every
        target actually holds the same graph.
        """
        # Two statements rather than one CALL {} subquery: Memgraph does not
        # implement COUNT subqueries, and this adapter serves Memgraph too.
        # Portable text everywhere beats clever text that needs a branch.
        nodes = self._run("MATCH (n) RETURN count(n) AS c", {})
        rels = self._run("MATCH ()-[r]->() RETURN count(r) AS c", {})

        notes: dict[str, Any] = {"dialect": self.dialect, "uri_scheme": self._uri.split(":")[0]}

        return Footprint(
            node_count=nodes[0]["c"] if nodes else None,
            relationship_count=rels[0]["c"] if rels else None,
            stored_bytes=None,  # not observable on any of these free tiers
            memory_bytes=None,
            notes=notes,
        )


# ── batching helpers ───────────────────────────────────────────────────────
#
# Shared by every Bolt target, so the batches each engine receives are
# identical by construction rather than by inspection. If these were
# per-adapter, "the same data went in the same way" would be a claim to audit;
# here it is a property of the code.


def _clean(row: dict[str, str], skip: tuple[str, ...] = ()) -> dict[str, str]:
    """Drop empty values from a CSV row.

    An absent property and a property set to the empty string are different
    graphs, and engines disagree about how they store the latter. Dropping
    blanks uniformly means every target holds the identical graph; doing it
    here rather than per-adapter is what makes that true without an audit.
    """
    return {k: v for k, v in row.items() if k not in skip and v != ""}


def _batched_nodes(path: str, batch_size: int) -> Iterator[tuple[str, list[dict[str, str]]]]:
    """Yield (label, rows) batches from the prepared node CSV."""
    buffers: dict[str, list[dict[str, str]]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            label = row["label"]
            buffer = buffers.setdefault(label, [])
            buffer.append(_clean(row, skip=("label",)))
            if len(buffer) >= batch_size:
                yield label, buffer
                buffers[label] = []
    for label, buffer in buffers.items():
        if buffer:
            yield label, buffer


def _batched_rels(
    path: str, batch_size: int
) -> Iterator[tuple[tuple[str, str, str], list[dict[str, str]]]]:
    """Yield ((start_label, rel_type, end_label), rows) batches.

    Grouped on all three because none of them can be a query parameter in
    Cypher -- a label and a relationship type are part of the statement text.
    """
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
