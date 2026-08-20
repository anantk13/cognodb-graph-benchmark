"""Contract tests for the adapter interface.

These do not touch a database. They check the properties that make the
comparison fair -- properties which, when they broke, broke silently.

The `clear` test exists because its absence cost a run. Containers get a fresh
container and the embedded engine gets a fresh directory, so both start empty
whatever the harness does; a managed service keeps whatever the last run left.
The second run against CognoDB stacked another full copy of the graph on top of
the first, reaching 300,236 nodes where the manifest says 161,236, and the
connection dropped mid-load. Nothing in the type system stopped that.
"""

from __future__ import annotations

import inspect

import pytest

from gbench.adapters.base import Adapter, Footprint, LoadResult, QueryResult
from gbench.adapters.bolt import BoltAdapter
from gbench.adapters.falkor import FalkorAdapter
from gbench.adapters.kuzu_embedded import KuzuAdapter

ADAPTERS = [BoltAdapter, FalkorAdapter, KuzuAdapter]
LIFECYCLE = ["connect", "clear", "create_schema", "load", "execute", "footprint", "close"]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.__name__)
class TestEveryAdapter:
    def test_implements_the_full_lifecycle(self, adapter: type[Adapter]) -> None:
        missing = [
            name
            for name in LIFECYCLE
            if getattr(adapter, name, None) is getattr(Adapter, name, object())
        ]
        assert not missing, f"{adapter.__name__} does not implement: {missing}"

    def test_is_instantiable(self, adapter: type[Adapter]) -> None:
        """An adapter left abstract fails at run time, not at import time."""
        assert not inspect.isabstract(adapter), (
            f"{adapter.__name__} is still abstract; an unimplemented method would "
            f"only surface mid-run"
        )

    def test_does_not_override_the_pool_size(self, adapter: type[Adapter]) -> None:
        """Pool size is the fairness constant.

        A published comparison was retracted after it emerged that one engine
        got a pool of 1 while every other got 25. Here it lives on the base
        class, and an adapter that shadowed it would recreate exactly that.
        """
        assert "pool_size" not in vars(adapter), (
            f"{adapter.__name__} overrides pool_size; it must come from the base "
            f"class so every target gets the identical value"
        )


class TestClearIsPartOfTheContract:
    def test_base_class_declares_it_abstract(self) -> None:
        assert "clear" in Adapter.__abstractmethods__

    def test_an_adapter_without_clear_cannot_be_instantiated(self) -> None:
        """The regression guard. Before `clear` existed, a target that never
        emptied itself was constructible and ran happily against a doubled
        graph."""

        class Forgetful(Adapter):
            name = "forgetful"
            dialect = "cypher5"

            def connect(self) -> None: ...
            def create_schema(self, indexes: list[tuple[str, str]]) -> None: ...
            def load(self, nodes_csv: str, rels_csv: str, batch_size: int) -> LoadResult: ...
            def execute(self, query: str, params: dict) -> QueryResult: ...
            def footprint(self) -> Footprint: ...
            def close(self) -> None: ...

        with pytest.raises(TypeError, match="clear"):
            Forgetful()  # type: ignore[abstract]


class TestQueryResult:
    def test_overhead_isolates_network_from_server(self) -> None:
        result = QueryResult(client_ms=237.3, server_ms=0.0, rows=1)
        assert result.overhead_ms == pytest.approx(237.3)

    def test_overhead_is_none_when_the_target_cannot_report(self) -> None:
        """Kuzu is embedded and Memgraph does not populate the Bolt field.
        Neither may be given a fabricated server time."""
        assert QueryResult(client_ms=1.0, server_ms=None, rows=1).overhead_ms is None

    def test_overhead_never_goes_negative(self) -> None:
        """Server time can exceed the client's on a 1 ms-resolution clock."""
        assert QueryResult(client_ms=0.4, server_ms=1.0, rows=1).overhead_ms == 0.0


class TestLoadResult:
    def test_rates_derive_from_wall_clock(self) -> None:
        result = LoadResult(
            wall_clock_s=40.0,
            nodes_loaded=161_236,
            relationships_loaded=381_523,
            batch_size=1000,
            method="driver batching",
        )
        assert result.nodes_per_second == pytest.approx(4030.9)
        assert result.relationships_per_second == pytest.approx(9538.075)

    def test_zero_duration_does_not_divide_by_zero(self) -> None:
        result = LoadResult(0.0, 1, 1, 1000, "x")
        assert result.nodes_per_second == 0.0


class TestFootprint:
    def test_unknowns_stay_none(self) -> None:
        """The brief asks for resource usage "where observable" and for
        "not observable" where it is not. None is how that is recorded; it must
        never be filled in with an estimate."""
        footprint = Footprint()
        assert footprint.stored_bytes is None
        assert footprint.memory_bytes is None
