"""The seven workloads, in every dialect, against the Appleby subgraph.

Two constraints shape every query here.

**Every query carries an explicit LIMIT.** CognoDB's console reports a hard
`Max result rows: 50,000` -- a limit published in no documentation, no pricing
page and no third-party write-up; it is visible only in the instance's own
Specifications panel. A three-hop traversal from a high-degree Officer in this
graph exceeds that easily. Without a uniform LIMIT, one target would silently
truncate where the others did not, and the comparison would be measuring the
cap rather than the engine. `RESULT_LIMIT` is applied identically everywhere.

**No traversal uses DISTINCT.** This is not a stylistic choice. Measured on
this dataset, the three-hop query written as `RETURN DISTINCT … LIMIT 1000`
returns 1000 rows on Neo4j and FalkorDB and **184 on Kuzu** -- because Kuzu
pushes the limit below the deduplication, taking 1000 of the 6,678 paths and
then deduplicating those to 184, where the others deduplicate 6,678 down to
1,182 and then take 1000. Rewriting it as `WITH DISTINCT … RETURN … LIMIT`
does not help; the optimiser pushes the limit past the explicit barrier too.

Byte-identical query text is therefore *necessary but not sufficient* for the
same logical query. Without DISTINCT every engine returns exactly
`min(paths, LIMIT)` and the workload is unambiguous, so that is what these
traversals do -- they count paths rather than distinct endpoints. Only the
row-count cross-check made the divergence visible at all.

**Start nodes are drawn from nodes that actually have edges.** Two thirds of
this graph's nodes are Addresses and Officers with a single relationship;
drawing start nodes uniformly at random would make most traversals return
nothing, and a benchmark of empty result sets measures dispatch overhead rather
than traversal. Pools are built from the relationship file, so every start node
has at least one edge, and the pools are seeded so every target traverses the
identical nodes in the identical order.

Where a dialect's text is byte-identical to CYPHER5 -- which, for these
queries, is most of them -- `rewrite_reason` is None and the README says so.
That is a stronger claim than a rewrite, and it is worth being able to make.
"""

from __future__ import annotations

import csv
import random
from collections import Counter
from pathlib import Path

from gbench.workloads.registry import Category, Dialect, Registry, Variant, Workload

#: Applied to every read workload on every target. See the module docstring.
RESULT_LIMIT = 1000

#: Jurisdictions drawn for the filtered lookup. Restricted to values that
#: actually occur, so the workload measures an index hit rather than an empty
#: scan; a filtered lookup that always misses is not a lookup benchmark.
_MIN_JURISDICTION_FREQ = 50


def _same(query: str) -> dict[Dialect, Variant]:
    """A workload whose text is identical in every dialect.

    Being able to say "the same characters ran on all five engines" is the
    strongest form of the same-logical-query claim, so it is worth making the
    identical case explicit rather than copy-pasting four variants.
    """
    return {d: Variant(query=query) for d in Dialect}


#: Minimum out-degree for a node to be eligible as a traversal start point.
#:
#: Chosen from the data, not picked to flatter a result. This graph's Officers
#: have a mean out-degree of 4.3 but a long tail: 76,913 have at least one edge,
#: yet only 46% have three or more. Sampling uniformly from all of them, as the
#: first version of this harness did, produced a modal row count of 1 for both
#: the one-hop and two-hop workloads -- and a two-hop query that ran *faster*
#: than the one-hop query, because neither was traversing anything. That
#: measures dispatch overhead wearing a traversal label.
#:
#: A threshold of 3 leaves 35,456 eligible Officers, seven times the sample
#: actually drawn, so the pool stays diverse. The opposite failure is equally
#: real: one published vendor benchmark ran every traversal from a single
#: hard-coded high-degree node, which measures one path rather than the graph.
MIN_START_DEGREE = 3


def build_pools(build_dir: Path, seed: int = 20260820) -> dict[str, list[str]]:
    """Sample start nodes and filter values from the prepared dataset.

    Returns pools keyed by label plus a `jurisdictions` pool. Sampling is
    seeded and the candidate set sorted before sampling, so the pools are
    identical on every machine, every run, and every target.
    """
    nodes_path = build_dir / "nodes.csv"
    rels_path = build_dir / "rels.csv"
    if not nodes_path.exists() or not rels_path.exists():
        raise FileNotFoundError(f"prepared dataset missing in {build_dir}; run `make data`")

    label_of: dict[str, str] = {}
    jurisdictions: Counter[str] = Counter()
    with nodes_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            label_of[row["node_id"]] = row["label"]
            if row["label"] == "Entity" and row["jurisdiction"]:
                jurisdictions[row["jurisdiction"]] += 1

    # Out-degree is counted per relationship type, not in total.
    #
    # Counting total degree was the first fix and it was not enough: an Officer
    # with three edges may have one OFFICER_OF and two REGISTERED_ADDRESS, so a
    # workload traversing OFFICER_OF still saw a single row. Measured, the
    # one-hop workload then cost 1.16 ms against a point lookup's 1.15 ms --
    # identical, because it was doing the same amount of work.
    #
    # A pool is therefore built per (label, relationship type). Traversal
    # workloads draw from the pool for the type they actually follow.
    total_degree: Counter[str] = Counter()
    typed_degree: dict[tuple[str, str], Counter[str]] = {}
    with rels_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            start = row["start_id"]
            total_degree[start] += 1
            key = (row["start_label"], row["rel_type"])
            typed_degree.setdefault(key, Counter())[start] += 1

    rng = random.Random(seed)
    pools: dict[str, list[str]] = {}

    def sample(candidates: list[str], key: str) -> None:
        ordered = sorted(candidates)
        pools[key] = rng.sample(ordered, min(5000, len(ordered)))

    by_label: dict[str, list[str]] = {}
    for node_id, degree in total_degree.items():
        if degree >= MIN_START_DEGREE and (label := label_of.get(node_id)):
            by_label.setdefault(label, []).append(node_id)
    for label, ids in by_label.items():
        sample(ids, label)

    for (label, rel_type), counts in typed_degree.items():
        eligible = [n for n, d in counts.items() if d >= MIN_START_DEGREE]
        if eligible:
            sample(eligible, f"{label}:{rel_type}")

    pools["jurisdictions"] = sorted(
        j for j, n in jurisdictions.items() if n >= _MIN_JURISDICTION_FREQ
    )
    return pools


def build_registry(pools: dict[str, list[str]]) -> Registry:
    """Assemble every workload, bound to the sampled parameter pools."""
    officers = pools.get("Officer") or []
    entities = pools.get("Entity") or []
    juris = pools.get("jurisdictions") or []

    # Traversal start nodes come from the OFFICER_OF-specific pool: every one
    # of them has at least MIN_START_DEGREE edges of the type the traversal
    # actually follows, so a one-hop query returns a neighbourhood rather than
    # a single row.
    traversers = pools.get("Officer:OFFICER_OF") or officers

    if not officers or not entities or not juris:
        raise ValueError(
            "parameter pools are empty; the prepared dataset does not match the "
            "expected schema (labels Officer/Entity, Entity.jurisdiction)"
        )

    registry = Registry(
        indexes=[
            ("Entity", "node_id"),
            ("Officer", "node_id"),
            ("Address", "node_id"),
            ("Entity", "jurisdiction"),
        ]
    )

    # ── lookups ────────────────────────────────────────────────────────────
    registry.add(
        Workload(
            id="point_lookup",
            category=Category.LOOKUP,
            description="Fetch one Officer by its indexed node_id.",
            variants=_same("MATCH (o:Officer {node_id: $id}) RETURN o.name AS name"),
            params=lambda rng: {"id": rng.choice(officers)},
            notes="Single-row result; measures index seek plus round trip.",
        )
    )

    registry.add(
        Workload(
            id="filtered_lookup",
            category=Category.LOOKUP,
            description=(
                "Fetch Entities in one jurisdiction, using the index on "
                "Entity.jurisdiction, capped at the uniform result limit."
            ),
            variants=_same(
                "MATCH (e:Entity) WHERE e.jurisdiction = $jurisdiction "
                f"RETURN e.node_id AS id, e.name AS name LIMIT {RESULT_LIMIT}"
            ),
            params=lambda rng: {"jurisdiction": rng.choice(juris)},
            notes=(
                "The index on Entity.jurisdiction is created on every target "
                "before measurement. An unindexed property on one engine and an "
                "indexed one on another is the single largest unearned speedup "
                "in the published graph-benchmark literature."
            ),
        )
    )

    # ── traversals ─────────────────────────────────────────────────────────
    registry.add(
        Workload(
            id="hop1",
            category=Category.TRAVERSAL,
            description="Entities one hop from a given Officer.",
            variants=_same(
                "MATCH (o:Officer {node_id: $id})-[:OFFICER_OF]->(e:Entity) "
                f"RETURN e.node_id AS id LIMIT {RESULT_LIMIT}"
            ),
            params=lambda rng: {"id": rng.choice(traversers)},
        )
    )

    registry.add(
        Workload(
            id="hop2",
            category=Category.TRAVERSAL,
            description=(
                "Co-officers: everyone who is an officer of any entity this "
                "officer is an officer of."
            ),
            variants=_same(
                "MATCH (o:Officer {node_id: $id})-[:OFFICER_OF]->(:Entity)"
                "<-[:OFFICER_OF]-(other:Officer) "
                f"RETURN other.node_id AS id LIMIT {RESULT_LIMIT}"
            ),
            params=lambda rng: {"id": rng.choice(traversers)},
        )
    )

    registry.add(
        Workload(
            id="hop3",
            category=Category.TRAVERSAL,
            description=(
                "Entities three hops away: the other entities that this "
                "officer's co-officers are themselves officers of."
            ),
            variants=_same(
                "MATCH (o:Officer {node_id: $id})-[:OFFICER_OF]->(:Entity)"
                "<-[:OFFICER_OF]-(:Officer)-[:OFFICER_OF]->(e2:Entity) "
                f"RETURN e2.node_id AS id LIMIT {RESULT_LIMIT}"
            ),
            params=lambda rng: {"id": rng.choice(traversers)},
            notes=(
                "The workload where engines diverge most, and the one most "
                "likely to hit CognoDB's 50,000-row ceiling without the "
                "uniform LIMIT. All three hop workloads expand along "
                "OFFICER_OF, the densest relationship in the graph at 240,495 "
                "edges. An earlier design hopped Officer -> Entity -> Address, "
                "which measured *fewer* rows at two hops than at one, because "
                "only 38,872 Entity -> Address edges exist and most entities "
                "have no registered address. A traversal benchmark whose "
                "two-hop query is cheaper than its one-hop query is measuring "
                "sparsity, not traversal."
            ),
        )
    )

    # ── aggregation ────────────────────────────────────────────────────────
    registry.add(
        Workload(
            id="aggregation",
            category=Category.AGGREGATION,
            description="Count Entities grouped by jurisdiction, most frequent first.",
            variants={
                Dialect.CYPHER5: Variant(
                    "MATCH (e:Entity) WHERE e.jurisdiction <> '' "
                    "RETURN e.jurisdiction AS jurisdiction, count(*) AS n "
                    f"ORDER BY n DESC LIMIT {RESULT_LIMIT}"
                ),
                Dialect.CYPHER_MEMGRAPH: Variant(
                    "MATCH (e:Entity) WHERE e.jurisdiction <> '' "
                    "RETURN e.jurisdiction AS jurisdiction, count(*) AS n "
                    f"ORDER BY n DESC LIMIT {RESULT_LIMIT}"
                ),
                Dialect.OPENCYPHER9: Variant(
                    "MATCH (e:Entity) WHERE e.jurisdiction <> '' "
                    "RETURN e.jurisdiction AS jurisdiction, count(*) AS n "
                    f"ORDER BY n DESC LIMIT {RESULT_LIMIT}"
                ),
                Dialect.CYPHER_KUZU: Variant(
                    "MATCH (e:Entity) WHERE e.jurisdiction <> '' "
                    "RETURN e.jurisdiction AS jurisdiction, count(*) AS n "
                    f"ORDER BY n DESC LIMIT {RESULT_LIMIT}"
                ),
            },
            params=None,
            notes=(
                "Unparameterised and therefore identical on every iteration, so "
                "this is the workload where caching effects show up most "
                "clearly. Reported with the warm-up curve for that reason."
            ),
        )
    )

    # ── write, for the mixed workload only ─────────────────────────────────
    registry.add(
        Workload(
            id="write_tag",
            category=Category.MIXED,
            description=(
                "Set a benchmark timestamp property on one Officer. A small, "
                "idempotent, index-hitting write."
            ),
            variants=_same(
                "MATCH (o:Officer {node_id: $id}) SET o.bench_seq = $seq RETURN o.node_id AS id"
            ),
            params=lambda rng: {
                "id": rng.choice(officers),
                "seq": rng.randint(0, 2**31),
            },
            writes=True,
            notes=(
                "Deliberately mutates an existing property rather than creating "
                "nodes: a write workload that grows the graph would change the "
                "dataset mid-run, so later reads would not be measuring the same "
                "graph as earlier ones."
            ),
        )
    )

    return registry
