"""One logical workload, several dialects.

The methodology rule this module exists to enforce is "same logical queries on
every platform". That is easy to claim and hard to verify, because the query
text is not identical everywhere -- engines that all advertise Cypher still
differ. Memgraph has no `shortestPath()` and expects a `*BFS` expansion.
FalkorDB implements openCypher 9, which lacks constructs Neo4j 5 has. Kuzu
requires a declared schema.

So a workload is defined once, in terms of what it asks, and carries a mapping
from dialect to the text that asks it. A reviewer reads one `Workload` and sees
every variant side by side, which is the only way to check that a rewrite for
one engine did not quietly become an easier question. Each variant also declares
`equivalent_rows`, and the runner compares actual row counts across targets: a
query returning 12 rows on one engine and 0 on another is not the same query,
however similar the text looks.

What must never happen is a dialect difference living in an adapter. Adapters
execute text; they do not compose it.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Category(str, Enum):
    """The metric categories the benchmark is required to report."""

    INGEST = "ingest"
    TRAVERSAL = "traversal"
    LOOKUP = "lookup"
    AGGREGATION = "aggregation"
    MIXED = "mixed"


class Dialect(str, Enum):
    """Query dialects, one per family of engine.

    These are not "one per database" by accident. CognoDB, Neo4j AuraDB and
    Neo4j Community all take CYPHER5 unchanged -- so they genuinely do run
    identical text, and the results table can say so without qualification.
    """

    CYPHER5 = "cypher5"
    """Neo4j 5 and anything Bolt-compatible with it: CognoDB c0, Aura, Neo4j."""

    CYPHER_MEMGRAPH = "cypher_memgraph"
    """Memgraph's Cypher: no shortestPath(), no exists(), no COUNT subqueries."""

    OPENCYPHER9 = "opencypher9"
    """FalkorDB, via the RedisGraph lineage."""

    CYPHER_KUZU = "cypher_kuzu"
    """Kuzu's Cypher over a declared schema."""


@dataclass(frozen=True)
class Variant:
    """The text of one workload in one dialect, and why it differs."""

    query: str

    rewrite_reason: str | None = None
    """Why this differs from the CYPHER5 text, if it does. Rendered verbatim in
    the README so that every rewrite is visible rather than buried in code.
    None means the text is byte-identical to the CYPHER5 variant."""

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("variant query is empty")


@dataclass(frozen=True)
class Workload:
    """A question, asked identically of every target."""

    id: str
    category: Category
    description: str
    """Plain-language statement of what is being asked, e.g. 'all entities two
    hops from a randomly chosen officer'. This is the definition of the
    workload; the dialect texts are implementations of it."""

    variants: dict[Dialect, Variant]

    params: Callable[[random.Random], dict[str, Any]] | None = None
    """Draws one parameter set per iteration from a seeded generator. Seeded so
    the same start nodes are used against every target -- a benchmark that
    reused a single hard-coded start node was one of the criticisms levelled at
    a published vendor comparison, and a benchmark that drew *different* random
    nodes per target would be worse still."""

    writes: bool = False
    """True if the workload mutates the graph. Write workloads are excluded from
    the read-latency tables and used only in the mixed workload."""

    notes: str = ""
    """Any caveat that belongs beside this workload's numbers."""

    def for_dialect(self, dialect: Dialect) -> Variant:
        """The text to run against a target speaking `dialect`.

        Raises rather than silently falling back to CYPHER5: an engine quietly
        running a dialect it does not fully support is exactly how a benchmark
        ends up measuring a different question on one target.
        """
        try:
            return self.variants[dialect]
        except KeyError:
            raise KeyError(
                f"workload {self.id!r} has no {dialect.value} variant; "
                f"defined for: {sorted(d.value for d in self.variants)}"
            ) from None

    def dialects_differ(self) -> bool:
        """True if any dialect required a rewrite. Drives a README column."""
        return any(v.rewrite_reason is not None for v in self.variants.values())


@dataclass
class Registry:
    """All workloads, plus the schema they assume."""

    workloads: list[Workload] = field(default_factory=list)

    indexes: list[tuple[str, str]] = field(default_factory=list)
    """(label, property) pairs created identically on every target before any
    measurement. Listed in the README. An unindexed property on one engine and
    an indexed one on another produced a widely-quoted 40x result that did not
    survive review."""

    def add(self, workload: Workload) -> Workload:
        if any(w.id == workload.id for w in self.workloads):
            raise ValueError(f"duplicate workload id {workload.id!r}")
        self.workloads.append(workload)
        return workload

    def by_category(self, category: Category) -> list[Workload]:
        return [w for w in self.workloads if w.category is category]

    def get(self, workload_id: str) -> Workload:
        for w in self.workloads:
            if w.id == workload_id:
                return w
        raise KeyError(f"no workload {workload_id!r}")

    def read_workloads(self) -> list[Workload]:
        return [w for w in self.workloads if not w.writes]

    def coverage_gaps(self, dialects: Sequence[Dialect]) -> dict[str, list[str]]:
        """Workloads missing a variant for one of `dialects`.

        Called before a run starts. A missing variant means that target cannot
        answer that question, which is a result worth reporting -- but it has to
        be known up front, not discovered as a stack trace at iteration 400.
        """
        gaps: dict[str, list[str]] = {}
        for w in self.workloads:
            missing = [d.value for d in dialects if d not in w.variants]
            if missing:
                gaps[w.id] = missing
        return gaps
