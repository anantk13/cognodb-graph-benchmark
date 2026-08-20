# I benchmarked five graph databases and mostly measured the network

I set out to compare CognoDB Cloud against four other graph databases on equal
footing. The first useful number I got was this one:

```
query: RETURN 1
  client-side:  237 ms
  server-side:    0 ms
```

That is a query which does nothing, against a managed graph database on a free
tier. The database spent no measurable time on it. The other 237 milliseconds
were my laptop in Bengaluru talking to a datacentre in Virginia.

If I had run the benchmark, published a latency table, and moved on, every
number in it would have been a map of undersea cable routes with a database's
name on top. That is roughly what a lot of published benchmarks in this space
are, and it is worth explaining how easy it is to end up there.

---

## The plan that didn't survive contact

The brief was straightforward: same dataset, same queries, same hardware, five
databases, report the percentiles. The fairness rule was explicit — *"Run every
database on the same resources: the same vCPU, RAM and storage allocation for
every platform, so no database gets a hardware advantage."*

So: use every platform's free tier, note the specs, done.

Except the free tiers do not agree on anything. Here is what I found in August
2026, checking each vendor's own pages:

| Platform | What "free" actually means |
|---|---|
| CognoDB c0 | 512 MB, 0.5 vCPU burst, 1 GiB disk — persistent |
| Neo4j AuraDB Free | node and relationship capped — persistent |
| FalkorDB Cloud | 100 MB, no persistence, stopped after 1 idle day, **deleted at 7** |
| Memgraph Cloud | 14-day trial, 2 GB |
| Arango | 14-day trial, deployment **auto-deleted** after |
| NebulaGraph Cloud | 14-day trial, **data deleted** after |
| TigerGraph | free tier withdrawn November 2024 — credits only |

**Two of seven offer a free tier that survives a week.** The rest are trials
that delete your work, and one is a 100 MB toy.

Those tiers span 100 MB to 4 GB. Putting them in one table and calling it a
resource-controlled comparison is not a rounding error; it is the exact
methodology mistake the brief warned about. You cannot run this benchmark the
obvious way, because the thing the obvious way requires does not exist any more.

That is finding number one, and I did not expect it.

---

## So: rent the hardware instead

If the platforms will not give me equal resources, I can impose them. Docker
enforces CPU and memory limits through cgroups, so every engine can be put in an
identical box:

```
--cpus=0.5 --memory=512m --memory-swap=512m
```

That last flag matters more than it looks. Leave `--memory-swap` unset and
Docker grants swap equal to the memory limit *again* — a container asked for
512 MB quietly receives a gigabyte of addressable memory, and your entire
resource sweep measures something other than what it says on the label.

And a limit you request is not a limit you got. So the harness reads the values
back from inside the running container:

```
$ docker exec … cat /sys/fs/cgroup/cpu.max
50000 100000                              # = exactly 0.5 CPU
$ docker exec … cat /sys/fs/cgroup/memory.max
536870912                                 # = exactly 512 MiB
```

Those two lines go into every result file. A reader should not have to take my
word for the cap.

This became the **primary** arm: identical envelope, no network in the path,
which is where "same resources" is actually achievable. The managed free tiers
became a secondary arm answering a different and equally real question — what
does a developer actually get for nothing — reported separately and never
averaged with the first.

---

## Finding two: at CognoDB's own free-tier size, most engines won't start

CognoDB's free tier advertises 512 MB. I put four other engines in a container
with that same limit.

| Engine at 512 MB | Result |
|---|---|
| Neo4j 5.26 Community | **OOM-killed**, exit 137 |
| Memgraph — `memgraph-mage`, the vendor's recommended image | **OOM-killed**, exit 137 |
| Memgraph — slim `memgraph` image | boots, serving in ~10 s |
| FalkorDB | boots, serving in ~5 s |

Neo4j's Docker image ships a 512 MB heap *plus* a 512 MB page cache — more than
a gigabyte before the JVM's own overhead — against a documented 2 GB minimum.
Even tuned down to fit, the JVM couldn't live in the box.

The detail I keep thinking about is this: **the last line in Neo4j's log before
the kernel killed it was `Started.`** It reported successful startup and then
died. A readiness check that watched the log — which is the obvious way to write
one — would have recorded a dead container as a healthy target and then reported
zero results for it as though the queries had simply been fast.

The harness therefore waits for the engine to *answer a query*, never for its
log to say something encouraging.

### The image that made a vendor look bad at their own product

Memgraph's documentation recommends `memgraph-mage` as the default image. It is
2.92 GB, because it bundles the graph algorithms library. It gets OOM-killed at
512 MB.

The slim `memgraph` image is 915 MB and boots fine.

Had I used the documented default, I would have published *"Memgraph cannot run
at 512 MB"* — which is false. And the fairness argument runs the other way too:
MAGE is roughly Memgraph-plus-algorithms, the equivalent of Neo4j plus GDS. I
was running Neo4j **Community**, which has no GDS. Pairing the heavy build of
one product against the light build of the other was never apples to apples.

I caught that by accident, checking why a result looked odd. That is not a
comfortable thing to notice about your own harness.

---

## Finding three: the network is the measurement

Back to the 237 milliseconds.

The Bolt protocol reports `result_available_after` — how long the *server*
spent — separately from what the client observed. So the split is free, per
query, on every Bolt target:

```
client_total − result_available_after ≈ network + driver
```

Measured round-trip floors from my client, using `RETURN 1` through the real
driver and TLS, warmed up first:

| Target | Round-trip floor, p50 |
|---|---|
| CognoDB c0 (`us-east4`) | **236.8 ms** |
| Neo4j AuraDB Free (`us-central1`) | **85.6 ms** |

Both nominally US regions, 151 ms apart. I am not going to speculate about why —
peering, edge termination, who knows. The point is that if you rank two engines
on client-side latency across that gap, you have ranked their network paths.

So every latency in this report is published twice: raw, and with the server's
own execution time beside it. And the floor for that target is printed next to
both, so the reader can see how much of the number could ever have been the
database.

One honest limit: Bolt reports server time in whole milliseconds, so a
sub-millisecond query reports `0`. In the containerised arm, where queries run
in single-digit milliseconds, the split is bounded by that resolution. In the
managed arm, where the network is 240 ms, it does not matter at all.

---

## The bugs I shipped

A benchmark that reports no defects in its own design has probably not looked
for any. Three that this one found before publishing anything:

**The traversals were not traversing.** The one-hop and two-hop workloads both
returned a *modal row count of 1*, and the two-hop query ran **faster** than the
one-hop query. Most Officers in this graph have exactly one edge, and I had been
drawing start nodes uniformly at random, so both queries were measuring dispatch
overhead with a traversal-shaped name.

I fixed the sampling twice — a degree floor, then a per-relationship-type degree
floor — and it got better without getting right. The actual defect was the query.
Two hops went `Officer → Entity → Address`, and the graph has only 38,872
`Entity → Address` edges against 240,495 `Officer → Entity`. The traversal was
running into a sparse region of the graph. Redesigned to expand along the dense
relationship, row counts now go 3 → 30 → 1000 and latency is monotonic.

**None of that was visible from latency alone.** It was caught by recording row
counts beside every measurement — which is also the check that tells you whether
your "identical" query is returning the same thing on every engine.

**A statistics bug that changed a threshold I had already quoted.** Two functions
computing the minimum sample size for a bounded p95 confidence interval
disagreed, because one omitted the `+1` on the upper order statistic. Corrected,
the minimum is **110, not 73**.

Which produced an interesting consequence. The commonly-suggested "at least 100
iterations per read workload" is **ten samples short**. At n = 100, the upper
bound of the p95 interval does not exist — there is no observation in the sample
large enough to close it. A p95 quoted from a hundred iterations has no error bar
in either direction.

This harness runs a thousand, and publishes the interval next to every p95.

**A blank password passed validation.** Empty credentials are legitimate for
Memgraph, which runs unauthenticated, so blanks were accepted everywhere — and
an unfilled `.env` would have reached the driver and failed as an opaque
authentication error twenty minutes into a run.

---

## Things I deliberately did not do

**No p99.** The latency client is closed-loop: it issues the next request when
the previous one returns. When a server stalls for a second, N workers each
record one slow sample instead of the thousands of requests that should have
been issued during the stall. This is coordinated omission, and the measured
distortion is about 1× at p50, 1.5× at p95, and **20× or worse at p99**.

Publishing a p99 from a closed-loop client is not a judgement call, it is
wrong. So `Summary` has no `p99` field — not omitted from the report,
structurally absent from the type, so no future edit can publish one by
accident.

**No engine's fastest loader.** CognoDB's bulk path is a console screen you
click; the free tier has no API keys, so nothing about it can be scripted.
Aura's is `LOAD CSV` from a remote URL. Neo4j Community's is an offline
`neo4j-admin import` the managed targets cannot use. Three incompatible
mechanisms; an ingest column mixing them compares nothing. Every target gets
driver batching at the same batch size, and the faster path each engine offers
is listed as measured-but-not-used.

**No mean-free percentile table.** Mean and standard deviation are printed
beside p50 and p95, because omitting them is its own form of cherry-picking —
one published vendor benchmark showed a 120× p99 win while the competitor it
named had in fact won the mean, the median, p90 *and* p95.

---

## Standing on the shoulders of retracted benchmarks

Almost everything above is a lesson someone else paid for. Every row in this
table is a real, published, promoted graph database benchmark:

| What went wrong | Where |
|---|---|
| Every database got 25 connections; **Neo4j got 1** | ArangoDB, 2018 — publicly retracted in a post titled *"How We Wronged Neo4j and PostgreSQL"* |
| Neo4j Community run with **no index** on the filtered property; one hard-coded start node | TigerGraph |
| p99 published, mean hidden — the competitor had won p50, p90 and p95 | Memgraph Benchgraph |
| Benchmark harness BSL-licensed, so the competitors it named could not re-run it | Memgraph |
| Ran on a GitHub Actions runner / a 2009 HP DL360, undisclosed | FalkorDB / ArangoDB |
| `MERGE` without labels → full scans; query plan cache disabled | Dgraph, 2017 |

Read that list and the shape of the problem is clear: none of these are
fabrications. They are all *ordinary mistakes*, of exactly the kind I made three
of before I finished. The difference between an honest benchmark and a
misleading one is mostly the checks you build in before you know what the
answer is going to be.

Which is also why this harness is MIT-licensed and every number in it is
generated from raw per-iteration data by a command anyone can run. Including,
specifically, any vendor measured in it.

---

## Results, methodology, caveats

Full results matrix, the complete methodology, and a caveats document listing
everything that weakens a claim I make — including the three bugs above and the
things I could not verify at all:

**→ [github.com/anantk13/cognodb-graph-benchmark](https://github.com/anantk13/cognodb-graph-benchmark)**

If you find a mistake in it, I would genuinely like to know. That is rather the
point.
