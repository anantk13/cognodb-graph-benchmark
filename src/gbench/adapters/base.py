"""The contract every database target implements.

The point of this interface is fairness. Each target gets exactly the same
sequence of calls, in the same order, with the same parameters, and the runner
cannot tell the targets apart. Anything a target needs that is peculiar to it --
a different client library, a different index syntax, a different way of
counting stored bytes -- is hidden behind these methods rather than leaking
into the runner as a special case.

Two published benchmarks failed precisely here. One gave every database a
connection pool of 25 and the database it was comparing against a pool of 1.
Another indexed the property it filtered on for its own engine but not for the
competitor. Both were consequences of per-target code paths in the harness. A
single interface, with pool size and index creation named explicitly in it,
makes that class of mistake visible in review.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class QueryResult:
    """One executed query, timed from both ends of the wire.

    `client_ms` is what the caller experienced. `server_ms` is what the database
    reported about itself, where it is able to. The difference is network plus
    driver overhead, which is the quantity that has to be separated out before
    a managed cloud instance can be compared against a container on localhost.
    """

    client_ms: float
    """Wall-clock duration measured around the driver call, in milliseconds."""

    server_ms: float | None
    """Server-reported execution time, or None where the target cannot report it."""

    rows: int
    """Number of records returned. Compared across targets to catch silent
    divergence -- a query that returns 12 rows on one engine and 0 on another is
    not the same query, however similar the text looks."""

    @property
    def overhead_ms(self) -> float | None:
        """Network plus driver cost, where the target reports server time."""
        if self.server_ms is None:
            return None
        return max(0.0, self.client_ms - self.server_ms)


@dataclass(frozen=True)
class LoadResult:
    """Outcome of ingesting the dataset into one target."""

    wall_clock_s: float
    nodes_loaded: int
    relationships_loaded: int
    batch_size: int
    """Recorded because ingest throughput is meaningless without it. Held
    identical across targets so the number compares."""

    method: str
    """Human-readable description of how the data went in, e.g.
    'driver batching, UNWIND + CREATE'. Printed in the README next to the
    faster native path that was deliberately not used."""

    @property
    def nodes_per_second(self) -> float:
        return self.nodes_loaded / self.wall_clock_s if self.wall_clock_s else 0.0

    @property
    def relationships_per_second(self) -> float:
        return self.relationships_loaded / self.wall_clock_s if self.wall_clock_s else 0.0


@dataclass(frozen=True)
class Footprint:
    """Whatever the target will tell us about its own resource use.

    Every field is optional on purpose. The assignment asks for resource usage
    "where observable" and to say "not observable" where it is not, so an
    unknown is recorded as None and rendered as 'not observable' rather than
    quietly omitted or guessed at.
    """

    stored_bytes: int | None = None
    memory_bytes: int | None = None
    node_count: int | None = None
    relationship_count: int | None = None
    notes: dict[str, Any] = field(default_factory=dict)
    """Free-form target-specific observations, e.g. the enforced cgroup limits
    read from inside the container."""


class Adapter(ABC):
    """One database target.

    Lifecycle, in the order the runner calls it:

        connect() -> clear() -> create_schema() -> load() -> [ execute() xN ]
            -> footprint() -> close()

    Implementations must not cache results between `execute` calls, must not
    rewrite the query text they are given, and must not vary the connection pool
    size. Where an engine's dialect genuinely differs, the difference belongs in
    the workload registry as a named dialect, not in the adapter.
    """

    #: Identifier used in results files and README tables, e.g. "cognodb-c0".
    name: str

    #: Which dialect of the workload registry this target is served by.
    dialect: str

    #: Connection pool size. Held identical across every target -- see the
    #: module docstring for why this is a named constant rather than a default.
    #:
    #: Sized above the highest concurrency level in the sweep (40). At 40
    #: clients against a pool of 16, the mixed workload would be measuring
    #: workers queueing for connections, which is a property of the harness
    #: rather than of the database. CognoDB's free tier permits 200, so this
    #: fits every target without special-casing any of them.
    pool_size: int = 50

    @abstractmethod
    def connect(self) -> None:
        """Open the connection pool. Must not run any query beyond a liveness check."""

    @abstractmethod
    def create_schema(self, indexes: list[tuple[str, str]]) -> None:
        """Create the same logical indexes on every target.

        `indexes` is a list of (label, property) pairs. Every target receives
        the identical list; how each expresses it is the adapter's business.
        The list is recorded in the results so the README can state exactly
        what was indexed where.
        """

    @abstractmethod
    def clear(self) -> None:
        """Delete every node and relationship, so the load starts from empty.

        Containers get a fresh container per run and the embedded engine gets a
        fresh directory, so both are empty by construction. A managed service is
        not: it keeps whatever the last run left. Without this, a second run
        stacks another full copy of the graph on top of the first -- observed,
        300,236 nodes where the manifest says 161,236 -- and the ingest timing,
        every latency, and the disk footprint are all then measured against a
        graph that is not the dataset.
        """

    @abstractmethod
    def load(self, nodes_csv: str, rels_csv: str, batch_size: int) -> LoadResult:
        """Ingest the dataset, using driver batching at the given batch size.

        Deliberately not each engine's fastest path. Bulk importers differ far
        too much between engines for their timings to be comparable, and one of
        the targets has no bulk importer at all. The faster path each engine
        offers is documented in the README as measured-but-not-used.
        """

    @abstractmethod
    def execute(self, query: str, params: dict[str, Any]) -> QueryResult:
        """Run one query and time it. Called once per iteration by the runner."""

    @abstractmethod
    def footprint(self) -> Footprint:
        """Report whatever the target exposes about its own resource use."""

    @abstractmethod
    def close(self) -> None:
        """Release the pool. Must be safe to call after a failed connect()."""

    def warmup_query(self) -> tuple[str, dict[str, Any]]:
        """A trivial query used to measure the round-trip floor.

        Subtracting this from a measured latency gives the portion attributable
        to connection, TLS and protocol rather than to the database. Targets
        that report their own server time do not depend on this, but it is
        recorded for all of them so the two methods can be cross-checked.
        """
        return "RETURN 1 AS n", {}

    def __enter__(self) -> Adapter:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
