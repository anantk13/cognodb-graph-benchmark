# Methodology

## The problem this design exists to solve

The assignment asks for the same resources on every platform. That is not
achievable across managed free tiers, and it is worth being precise about why.

Of seven managed graph platforms surveyed in August 2026, **two still offer a
free tier that survives a week**:

| Platform | What "free" means | Persistent? |
|---|---|---|
| CognoDB c0 | 512 MB, 0.5 vCPU burst, 1 GiB disk | yes |
| Neo4j AuraDB Free | node/relationship capped | yes |
| FalkorDB Cloud | 100 MB, no persistence, deleted after 7 idle days | no |
| Memgraph Cloud | 14-day trial, 2 GB | no |
| Arango | 14-day trial, deployment auto-deleted | no |
| NebulaGraph Cloud | 14-day trial, data deleted | no |
| TigerGraph | free tier removed Nov 2024; credits only | no |

Those tiers span 100 MB to 4 GB. Comparing them directly and calling it a
resource-controlled benchmark would be the exact methodology error the brief
warns against.

## Two arms, both anchored on CognoDB's specification

**Arm A -- CognoDB's envelope, imposed on every other engine.** The cgroup
limits are not arbitrary: **0.5 vCPU and 512 MB is CognoDB c0's advertised
specification**, and every other engine is held to it. The arm asks a single
question -- what can a graph database do inside the envelope CognoDB ships as
its free tier -- and it is the primary result because it is the only place
"same resources everywhere" is actually achieved.

CognoDB itself cannot appear in this arm. It is a managed service with no
self-hosted distribution, so it runs on its real c0 instance rather than in a
container. That is a limitation of what can be containerised, not a choice
about emphasis: CognoDB sets the resource target the arm enforces.

**Arm B -- the managed tiers as shipped.** CognoDB c0 and Neo4j AuraDB Free,
the only two managed graph platforms surveyed that still offer a free tier
surviving a week. Explicitly *not* resource-equal, and network is in the path.
It answers what a developer actually receives.

The two arms are reported separately and never averaged. A container on
loopback and an instance 240 ms away are not comparable on client latency.

## What is held identical

| | How |
|---|---|
| Dataset | One prepared CSV pair, SHA-256 in `data/build/manifest.json`, loaded into every target |
| Queries | One workload registry; where a dialect required no rewrite, the text is byte-identical |
| Indexes | The same `(label, property)` list passed to every adapter's `create_schema` |
| Connection pool | A single constant on the base adapter class, 50, above the highest concurrency level |
| Load method | Driver batching, `UNWIND` + `CREATE`, identical batch size |
| Start nodes | Seeded generator; every target traverses the same nodes in the same order |
| Warm-up | Same iteration count, and the curve is published |

## Memory sweep rather than a single cap

Neo4j's Docker image ships a 512 MB heap plus a 512 MB page cache -- more than
1 GB before JVM overhead -- against a documented 2 GB minimum. Capping every
engine at CognoDB's advertised 512 MB would therefore have produced a DNF for
the single most important comparison in the study.

Rather than raise the cap for one engine, the cap is swept upward from
CognoDB's specification: **512 MB (CognoDB c0 parity), 1 GB, 2 GB**, with each engine's internal memory budget set to the *same fraction*
(55%) of its container limit. Left at their defaults those budgets are wildly
unequal -- Neo4j takes a fixed 512M + 512M regardless of the cgroup, Memgraph
takes 90-100% of detected RAM, Kuzu takes 80% -- and that inequality would
silently have become the result.

A DNF is published as a DNF, with its exit code and OOM status.

## Verifying the cap rather than asserting it

`--memory=512m` is a request. The harness reads `cpu.max` and `memory.max` from
`/sys/fs/cgroup` **inside the running container** and records both in the
results, so the published limit is the enforced one.

`--memory-swap` is set equal to `--memory` deliberately. Left unset, Docker
grants swap equal to the memory limit again, and a container asked for 512m
quietly receives 1 GB of addressable memory.

## Separating the network from the database

Arm B's targets are ~240 ms and ~86 ms away from the benchmark client. A query
taking 2 ms on the server costs 240 ms on the wire, so a client-side latency
from this arm is overwhelmingly a measurement of the network.

Two independent methods separate them:

1. **Server-reported execution time.** Bolt returns `result_available_after`
   and `result_consumed_after`; FalkorDB returns `run_time_ms`. Every latency
   is recorded with both the client and server figure.
2. **A round-trip floor.** `RETURN 1` through the same driver, TLS and session,
   warmed up first. This bounds what any query on that target can cost.

Measured: a `RETURN 1` against CognoDB c0 costs **237 ms client-side and 0 ms
server-side.** The floor is not a rounding error; on that target it *is* the
measurement.

Bolt reports server time in whole milliseconds, so sub-millisecond queries
report `0`. The split is therefore bounded by 1 ms of resolution, which matters
for Arm A and is irrelevant for Arm B.

## Statistics

**1000 iterations per workload, not the 100 commonly suggested.** A
distribution-free 95% confidence interval for p95 comes from the order
statistics `n*q ± 1.96*sqrt(n*q*(1-q))`, and the upper bound only lands inside
the sample at **n ≥ 110**. At n = 100 the interval does not close, so a p95
quoted from a hundred iterations has no error bar at all. Every published p95
carries its interval; where the interval is unbounded, the table says so.

**p50 and p95 only. No p99.** This client is closed-loop: it issues the next
request when the previous one returns, which under-samples stalls. Published
measurements put the distortion at roughly 1x at p50, 1.5x at p95 and 20x or
worse at p99. `Summary` has no `p99` field, so no future code can publish one
by accident.

**Mean and standard deviation are printed alongside.** Omitting them is its own
form of cherry-picking: one vendor benchmark published a 120x p99 win while the
competitor it named had in fact won the mean, p50, p90 and p95.

**The warm-up curve is published, not assumed.** Barrett et al. (OOPSLA 2017)
found only 43.5% of measured VM benchmark pairs ever reach a steady state and
~18% get slower over time. Every warm-up sample is retained in the raw results.

## No traversal uses DISTINCT

Measured on this dataset, the three-hop traversal written as
`RETURN DISTINCT ... LIMIT 1000` returns:

| Engine | Rows |
|---|---:|
| Neo4j | 1000 |
| FalkorDB | 1000 |
| Kuzu | **184** |

Same string, same start node, same graph. Asking the loaded Kuzu database
directly: `DISTINCT + LIMIT 1000` gives 184, `DISTINCT` with no limit gives
1182, and `count(*)` over all matching paths gives 6678. Kuzu pushes the limit
below the deduplication -- it takes 1000 of the 6678 paths and deduplicates
those to 184, where the others deduplicate 6678 to 1182 and then take 1000.

Rewriting it as `WITH DISTINCT e2 RETURN e2.node_id LIMIT 1000` does not help;
the optimiser pushes the limit past that barrier as well. There is no
formulation of `DISTINCT` plus `LIMIT` on which these engines agree.

Before attributing this to Kuzu, its traversal semantics were checked directly
on a hand-built five-node graph: forward traversal, reverse traversal, and
reverse traversal across a `REL TABLE GROUP` all returned exactly what Neo4j
would. The divergence is an evaluation-order difference, not an error, and the
query was ambiguous.

The traversals therefore drop `DISTINCT` and count paths rather than distinct
endpoints. Every engine then returns exactly `min(paths, LIMIT)`.

**Byte-identical query text is necessary but not sufficient for the same
logical query.** That is the single most useful thing this harness found.

## Row counts are compared across targets

Every workload records the distribution of row counts it returned. A query
returning 12 rows on one engine and 0 on another is not the same query, however
similar the text looks.

This check caught **four** real defects before any result was published: three
in this harness's own workload design, and one genuine semantic divergence
between two Cypher engines. In every case latency alone looked fine. See
`docs/CAVEATS.md`.
