"""Tests for the invariants that make "the same query ran everywhere" checkable.

The registry exists so that a dialect difference is visible in review rather
than buried in an adapter. These tests are what stop that guarantee eroding.
"""

from __future__ import annotations

import random

import pytest

from gbench.workloads.registry import (
    Category,
    Dialect,
    Registry,
    Variant,
    Workload,
)


def _workload(workload_id: str = "w", **overrides) -> Workload:
    defaults = {
        "id": workload_id,
        "category": Category.LOOKUP,
        "description": "test",
        "variants": {d: Variant(query="RETURN 1") for d in Dialect},
    }
    return Workload(**{**defaults, **overrides})


class TestDialectResolution:
    def test_missing_variant_raises_rather_than_falling_back(self) -> None:
        """A silent fallback to Cypher 5 would be the whole failure mode.

        An engine quietly running a dialect it does not fully support is how a
        benchmark ends up measuring a different question on one target while
        reporting it in the same column as the others.
        """
        workload = _workload(variants={Dialect.CYPHER5: Variant(query="RETURN 1")})
        assert workload.for_dialect(Dialect.CYPHER5).query == "RETURN 1"
        with pytest.raises(KeyError, match="no cypher_memgraph variant"):
            workload.for_dialect(Dialect.CYPHER_MEMGRAPH)

    def test_missing_variant_error_names_what_is_defined(self) -> None:
        workload = _workload(variants={Dialect.CYPHER5: Variant(query="RETURN 1")})
        with pytest.raises(KeyError) as exc:
            workload.for_dialect(Dialect.OPENCYPHER9)
        assert "cypher5" in str(exc.value)

    def test_identical_text_is_reported_as_identical(self) -> None:
        """The strongest form of the claim, so it must be distinguishable."""
        assert not _workload().dialects_differ()

    def test_a_rewrite_is_flagged(self) -> None:
        workload = _workload(
            variants={
                Dialect.CYPHER5: Variant(query="MATCH p=shortestPath((a)-[*]-(b)) RETURN p"),
                Dialect.CYPHER_MEMGRAPH: Variant(
                    query="MATCH p=(a)-[*BFS]-(b) RETURN p",
                    rewrite_reason="Memgraph has no shortestPath(); uses *BFS expansion",
                ),
            }
        )
        assert workload.dialects_differ()

    def test_empty_query_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            Variant(query="   ")


class TestRegistry:
    def test_duplicate_id_is_rejected(self) -> None:
        registry = Registry()
        registry.add(_workload("dup"))
        with pytest.raises(ValueError, match="duplicate"):
            registry.add(_workload("dup"))

    def test_coverage_gaps_are_found_before_a_run_starts(self) -> None:
        """Discovering a missing variant at iteration 400 wastes the run.

        A target that cannot answer a workload is a result worth reporting, but
        it has to be known up front rather than arriving as a stack trace.
        """
        registry = Registry(indexes=[("Entity", "node_id")])
        registry.add(_workload("full"))
        registry.add(
            _workload("partial", variants={Dialect.CYPHER5: Variant(query="RETURN 1")})
        )
        gaps = registry.coverage_gaps(list(Dialect))
        assert "full" not in gaps
        assert set(gaps["partial"]) == {d.value for d in Dialect if d is not Dialect.CYPHER5}

    def test_write_workloads_are_excluded_from_read_workloads(self) -> None:
        """Writes belong only in the mixed workload; a write in the read
        latency tables would compare a mutation against a lookup."""
        registry = Registry()
        registry.add(_workload("read"))
        registry.add(_workload("write", writes=True))
        assert [w.id for w in registry.read_workloads()] == ["read"]


class TestParameterGeneration:
    def test_same_seed_yields_the_same_sequence(self) -> None:
        """Every target must traverse the identical nodes in the identical order.

        Drawing different random nodes per target would mean the targets were
        not answering the same question at all.
        """
        pool = [str(i) for i in range(1000)]
        workload = _workload(params=lambda rng: {"id": rng.choice(pool)})
        first = [workload.params(random.Random(7))["id"] for _ in range(5)]
        second = [workload.params(random.Random(7))["id"] for _ in range(5)]
        assert first == second

    def test_a_sequence_from_one_generator_varies(self) -> None:
        """Seeded does not mean constant -- one hard-coded start node was a
        documented defect in a published vendor benchmark."""
        pool = [str(i) for i in range(1000)]
        workload = _workload(params=lambda rng: {"id": rng.choice(pool)})
        rng = random.Random(7)
        drawn = {workload.params(rng)["id"] for _ in range(50)}
        assert len(drawn) > 25
