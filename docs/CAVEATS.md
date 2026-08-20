# Caveats

Everything below weakens some claim in this report. It is collected in one
place because a caveat a reader has to discover is worth less than nothing.

## Things that limit the comparison

### The two managed targets are in different regions

The methodology rule asks for the same region everywhere. CognoDB offers
`us-east4`, `us-central1` and `europe-west1`; Neo4j AuraDB Free is GCP-only in
`europe-west1`, `us-central1` and `asia-southeast1`. The overlap is two
regions, and the CognoDB instance was provisioned in `us-east4` before that was
established. **A CognoDB instance's region cannot be changed after creation.**

Aura was therefore placed in `us-central1`, the nearest available region,
keeping both on the same continent.

What this does and does not affect: Arm A runs on loopback and is unaffected.
Server-reported execution times exclude the network by construction and are
unaffected. Only Arm B's raw client-side latencies carry the difference, and
the measured round-trip floor to each endpoint is published beside them.

Measured floors: **CognoDB `us-east4` p50 236.8 ms; Aura `us-central1` p50
85.6 ms.** The 151 ms gap is larger than geography alone would suggest. This
report does not speculate about why -- it publishes both floors and reports
every latency net of them.

### The benchmark client is in India; both managed targets are in the US

Round-trip time to Arm B dominates every client-side number in it. This is
stated as a finding rather than hidden as a limitation, but it does mean Arm B
cannot rank the two engines on raw client latency. Use the server-side column.

### Containers run inside a Linux VM on macOS

Arm A runs under Colima (Lima + Apple Virtualization) on macOS 15: an Ubuntu
24.04 guest with cgroup v2, given 8 vCPU and 11.65 GiB. The guest is
deliberately oversized relative to every container cap, so the cgroup limit is
the only binding constraint, and the enforced `cpu.max` / `memory.max` are read
from inside each container and published.

What cannot be claimed: that `--cpus=0.5` inside that guest equals half a
physical core of an Apple Silicon chip with asymmetric performance and
efficiency cores. Docker publishes no benchmark-accuracy statement for
virtualised hosts. **Relative comparisons between engines in Arm A are valid --
they all ran in the same guest, back to back, under the same limits. Absolute
throughput figures should not be assumed to transfer to bare metal.**

### Free tiers are multi-tenant and their neighbours are invisible

Another tenant's load can slow a measurement and there is no way to observe it
from outside. Unfixable at any price on a free tier. Mitigated only by
iteration count and by publishing variance.

### Server-side timing has 1 ms resolution

Bolt reports `result_available_after` as a whole number of milliseconds, so
sub-millisecond queries report `0`. In Arm A, where queries run in
single-digit milliseconds, the network/server split is bounded by that
resolution rather than by precision. In Arm B, where the network is ~240 ms, it
is irrelevant.

### No p99 is published

The latency client is closed-loop, which under-samples stalls. Published
measurements put the distortion at ~1x at p50, ~1.5x at p95, and 20x or worse
at p99. `Summary` has no p99 field. The open-loop generator in
`runner/concurrency.py` exists for the honest version of that question.

### Kuzu is not measuring the same thing

Kuzu is embedded: no server, no protocol, no network, no connection pool. It is
reported in its own table. Placed beside four client-server engines it would
win on latency by virtue of not being one.

### FalkorDB is driven over a different client

FalkorDB exposes a Bolt port, which would have let it share the Bolt adapter's
single code path. It is not used, because FalkorDB's own documentation
describes that support as experimental and not recommended for production.
The consequence is that FalkorDB's client-side overhead is not identical to the
four Bolt targets'. Its server-reported times are unaffected.

### Kuzu has no secondary index

Kuzu indexes a node table's primary key automatically and offers no secondary
index for `Entity.jurisdiction`. The filtered-lookup workload is therefore an
indexed seek on four engines and a scan on this one. That is a real property of
the engine, not a harness defect, but it means that row is not a like-for-like
comparison.

## Defects found in this harness before publishing

Listed because a benchmark that reports no bugs in its own design has probably
not looked.

**The traversals were not traversing.** The first design drew start nodes
uniformly at random. Most Officers in this graph have exactly one edge, so the
one-hop and two-hop workloads both returned a modal row count of 1 -- and the
two-hop query ran *faster* than the one-hop query. Two fixes to the sampling
(a total-degree floor, then a per-relationship-type degree floor) improved it
without solving it. The actual defect was the query: two hops went
`Officer -> Entity -> Address`, and only 38,872 `Entity -> Address` edges exist
against 240,495 `Officer -> Entity`, so the traversal ran into a sparse region.
Redesigned to expand along `OFFICER_OF`, row counts now go 3 → 30 → 1000 and
latency is monotonic. **None of this was visible from latency alone; it was
caught by recording row counts beside every measurement.**

**An engine returning a fifth of the rows, invisibly.** The three-hop
traversal returned 1000 rows on Neo4j and FalkorDB and 184 on Kuzu, from the
same string and the same start node, because Kuzu pushes `LIMIT` below
`DISTINCT`. Kuzu's three-hop latency looked *good* -- it was doing a fraction
of the work. Latency alone showed nothing wrong; only the row-count comparison
exposed it. The traversals now avoid `DISTINCT` entirely. See
`docs/METHODOLOGY.md` for the full measurement.

Worth recording how close this came to being published the wrong way round:
the first hypothesis was that Kuzu was wrong, the second that this harness's
Kuzu schema was wrong, and it was neither.

**A statistics bug that changed a published threshold.** Two functions
computing the minimum sample size for a bounded p95 interval disagreed, because
one omitted the `+1` on the upper order statistic. They now share one
implementation. The corrected minimum is **110, not 73** -- which is what
established that the commonly suggested "at least 100 iterations" does not
close the interval.

**A blank password passed validation.** Empty credentials are legitimate for
Memgraph, which runs unauthenticated, so blanks were accepted everywhere. An
unfilled `.env` would have reached the driver and failed as an opaque
authentication error mid-run. Targets now declare which credentials may be
blank, and template placeholders are rejected by name.

## Things that could not be verified

- **Neo4j Aura's cloud terms.** `neo4j.com` returns HTTP 403 to automated
  fetches, so the terms governing the managed service could not be read to
  check for a restriction on publishing benchmark results. Recorded as unknown
  rather than assumed permissive; the CLI prints a warning before every run.
  (The other four were read in full and are clear -- Memgraph's BSL 1.1 and
  FalkorDB's SSPL v1 restrict hosting or distributing the engine as a service
  and carry no DeWitt clause; CognoDB's terms of service are likewise clear.)
- **CognoDB's engine version via the wire.** `dbms.components()` does not
  exist on CognoDB, so there is no Cypher route to the version. The console
  reports `v0.9.11`; that is the only source.
- **CognoDB's node cap, query timeout and idle behaviour.** Published nowhere.
  Unknown failure modes mid-run.
- **Aura Free's runtime.** The kernel identifies as `5.27-aura, enterprise`.
  Whether the pipelined and parallel runtimes are enabled on the free tier is a
  separate question and is not established here.

## Where the assignment brief and the product disagree

Recorded because they were checked, not to score points.

| Brief says | Observed |
|---|---|
| c0 has **256 MB** RAM | Console and pricing page both say **512 MB** |
| Endpoint is `<id>.databases.cognodb.**cloud**` | `<id>.bravo.databases.cognodb.**com**` |
| Password is "shown exactly once -- copy it immediately" | Retrievable any time from the Connect tab |

One limit appears in no documentation, no pricing page and no third-party
write-up -- only in the instance's own Specifications panel:
**`Max result rows: 50,000`.** Every workload in this harness carries an
explicit `LIMIT` because of it.
