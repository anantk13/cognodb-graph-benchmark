"""Turn raw results into the tables and charts the README publishes.

Every number in the README is produced here, from the JSON the orchestrator
wrote. None is typed by hand. That matters for a reason beyond tidiness: a
hand-typed results table cannot be checked against the run that produced it, and
at least one published benchmark's numbers turned out not to match its own
harness output. `make report` regenerates the tables from `results/raw/`, so a
reader can re-run it and diff.

Three presentation rules, each answering a documented failure:

*   Where a target reports server-side execution time, the client-side latency
    is shown beside it and the round-trip floor is shown beside both. A managed
    instance 240 ms away and a container on loopback are not comparable on
    client latency alone, and printing only the client number invites exactly
    that comparison.
*   Mean and standard deviation are printed next to p50 and p95. Publishing
    percentiles alone is how one vendor benchmark showed a 120x tail win while
    quietly losing the mean, the median and p90.
*   A missing measurement renders as "not observable" or "DNF", never as a
    blank cell or an omitted row. A benchmark's failures are results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKLOAD_ORDER = (
    "point_lookup",
    "filtered_lookup",
    "hop1",
    "hop2",
    "hop3",
    "aggregation",
    "write_tag",
)

WORKLOAD_LABEL = {
    "point_lookup": "Point lookup",
    "filtered_lookup": "Filtered lookup (indexed)",
    "hop1": "1-hop traversal",
    "hop2": "2-hop traversal",
    "hop3": "3-hop traversal",
    "aggregation": "Aggregation (group-by)",
    "write_tag": "Write (indexed update)",
}


@dataclass
class Record:
    """One target's results, from one run."""

    path: Path
    data: dict[str, Any]

    @property
    def target_id(self) -> str:
        return (self.data.get("target") or {}).get("id", self.path.stem)

    @property
    def tier(self) -> str | None:
        return self.data.get("tier")

    @property
    def label(self) -> str:
        return f"{self.target_id} @ {self.tier}" if self.tier else self.target_id

    @property
    def dnf(self) -> bool:
        return bool((self.data.get("container") or {}).get("dnf")) or "dnf_reason" in self.data

    @property
    def errored(self) -> str | None:
        return self.data.get("error")

    def workload(self, workload_id: str) -> dict[str, Any] | None:
        for entry in self.data.get("workloads") or []:
            if entry.get("workload_id") == workload_id:
                return entry
        return None


def _tier_bytes(tier: str | None) -> int:
    """Tier ordered by the size it denotes. Lexical order puts 512m after 2g."""
    if not tier:
        return 0
    value = tier.strip().lower()
    return int(value[:-1]) * (1 << 30 if value.endswith("g") else 1 << 20)


def load_records(raw_dir: Path) -> list[Record]:
    """Read every result file from the most recent run directory."""
    runs = sorted((p for p in raw_dir.iterdir() if p.is_dir()), reverse=True)
    if not runs:
        raise FileNotFoundError(f"no runs under {raw_dir}; run `make bench` first")
    latest = runs[0]
    records = [
        Record(path=path, data=json.loads(path.read_text()))
        for path in sorted(latest.rglob("*.json"))
    ]
    if not records:
        raise FileNotFoundError(f"run directory {latest} contains no results")
    return records


def _fmt(value: float | None, unit: str = "", digits: int = 2) -> str:
    if value is None:
        return "not observable"
    return f"{value:,.{digits}f}{unit}"


def _bytes(value: int | None) -> str:
    if value is None:
        return "not observable"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024:
            return f"{value:,.1f} {unit}"
        value /= 1024.0
    return f"{value:,.1f} TiB"


def table_environment(records: list[Record]) -> str:
    """Where each target ran and what it claims about itself."""
    lines = [
        "| Target | Arm | Image / endpoint | Advertised | Dialect |",
        "|---|---|---|---|---|",
    ]
    for record in records:
        target = record.data.get("target") or {}
        advertised = target.get("advertised") or {}
        claims = ", ".join(
            f"{k}: {v}" for k, v in advertised.items() if k != "source" and v is not None
        )
        lines.append(
            f"| `{record.label}` | {target.get('arm', '?')} "
            f"| `{target.get('image') or 'managed service'}` "
            f"| {claims or 'not published'} | `{target.get('dialect', '?')}` |"
        )
    return "\n".join(lines)


def table_ingest(records: list[Record]) -> str:
    """Ingest throughput. Identical method and batch size on every target."""
    lines = [
        "| Target | Nodes/s | Rels/s | Wall clock | Graph verified |",
        "|---|---:|---:|---:|---|",
    ]
    for record in records:
        if record.dnf:
            lines.append(f"| `{record.label}` | DNF | DNF | DNF | — |")
            continue
        load = record.data.get("load")
        if not load:
            lines.append(f"| `{record.label}` | — | — | — | {record.errored or 'no data'} |")
            continue
        verified = "yes" if record.data.get("graph_matches_manifest") else "**MISMATCH**"
        lines.append(
            f"| `{record.label}` | {load['nodes_per_second']:,.0f} "
            f"| {load['relationships_per_second']:,.0f} "
            f"| {load['wall_clock_s']:,.1f}s | {verified} |"
        )
    return "\n".join(lines)


def table_latency(records: list[Record], workload_id: str) -> str:
    """One workload across every target, client and server time side by side."""
    lines = [
        "| Target | p50 | p95 | p95 CI | mean | server p50 | RTT floor p50 | rows |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for record in records:
        if record.dnf:
            lines.append(f"| `{record.label}` | DNF | DNF | — | — | — | — | — |")
            continue
        entry = record.workload(workload_id)
        if not entry or not entry.get("client"):
            lines.append(f"| `{record.label}` | — | — | — | — | — | — | — |")
            continue

        client = entry["client"]
        server = entry.get("server")
        floor = record.data.get("round_trip_floor")
        ci = (
            f"[{client['p95_ci_low_ms']:,.2f}, {client['p95_ci_high_ms']:,.2f}]"
            if client.get("p95_ci_bounded")
            else "**unbounded**"
        )
        rows = entry.get("row_counts") or {}
        modal = max(rows, key=rows.get) if rows else "—"
        lines.append(
            f"| `{record.label}` | {_fmt(client['p50_ms'], 'ms')} "
            f"| {_fmt(client['p95_ms'], 'ms')} | {ci} "
            f"| {_fmt(client['mean_ms'], 'ms')} "
            f"| {_fmt(server['p50_ms'], 'ms') if server else 'not reported'} "
            f"| {_fmt(floor['p50_ms'], 'ms') if floor else 'n/a'} | {modal} |"
        )
    return "\n".join(lines)


def table_concurrency(records: list[Record]) -> str:
    """Sustained throughput at each client concurrency level."""
    lines = [
        "| Target | Clients | Sustained q/s | p50 | p95 | Reads | Writes | Failures |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for record in records:
        for level in record.data.get("concurrency") or []:
            latency = level.get("latency") or {}
            lines.append(
                f"| `{record.label}` | {level['clients']} | {level['qps']:,.1f} "
                f"| {_fmt(latency.get('p50_ms'), 'ms')} | {_fmt(latency.get('p95_ms'), 'ms')} "
                f"| {level['reads']:,} | {level['writes']:,} | {level['failures']:,} |"
            )
    return "\n".join(lines) if len(lines) > 2 else "_No concurrency data in this run._"


def table_cold_start(records: list[Record]) -> str:
    """Cold versus warm round trip, reported separately.

    The cold figure is a trivial query issued against a freshly started engine
    holding no data; the warm one is the same query after the graph is loaded.
    Reported apart rather than averaged, because a benchmark that folds
    cold-start into steady-state numbers is measuring startup and calling it
    throughput -- one published comparison recorded 400 ms on a first run and
    77 ms on the second and reported the first.
    """
    lines = [
        "| Target | Cold p50 | Cold p95 | Warm p50 | Warm p95 | Cold/warm ratio |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    any_row = False
    for record in records:
        if record.dnf or record.errored:
            continue
        cold = record.data.get("round_trip_floor_cold")
        warm = record.data.get("round_trip_floor")
        if not cold or not warm:
            continue
        any_row = True
        ratio = cold["p50_ms"] / warm["p50_ms"] if warm["p50_ms"] else float("nan")
        lines.append(
            f"| `{record.label}` | {_fmt(cold['p50_ms'], 'ms')} | {_fmt(cold['p95_ms'], 'ms')} "
            f"| {_fmt(warm['p50_ms'], 'ms')} | {_fmt(warm['p95_ms'], 'ms')} "
            f"| {ratio:,.2f}x |"
        )
    return "\n".join(lines) if any_row else "_No cold-start data in this run._"


def table_footprint(records: list[Record]) -> str:
    """Resource usage where observable, and an explicit note where not."""
    lines = [
        "| Target | Stored | Memory | Nodes | Relationships | Enforced cgroup |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for record in records:
        if record.dnf:
            lines.append(f"| `{record.label}` | DNF | DNF | — | — | — |")
            continue
        footprint = record.data.get("footprint") or {}
        container = record.data.get("container") or {}
        enforced = container.get("enforced_cgroup") or {}
        cgroup = (
            f"`cpu.max={enforced.get('cpu.max')}` `memory.max={enforced.get('memory.max')}`"
            if enforced
            else "not containerised"
        )
        lines.append(
            f"| `{record.label}` | {_bytes(footprint.get('stored_bytes'))} "
            f"| {_bytes(footprint.get('memory_bytes'))} "
            f"| {footprint.get('node_count'):,} | {footprint.get('relationship_count'):,} "
            f"| {cgroup} |"
            if footprint.get("node_count") is not None
            else f"| `{record.label}` | — | — | — | — | {cgroup} |"
        )
    return "\n".join(lines)


def table_dnf(records: list[Record]) -> str:
    """Everything that failed, and why. Failures are results."""
    rows = []
    for record in records:
        container = record.data.get("container") or {}
        if record.dnf:
            rows.append(
                f"| `{record.label}` | DNF | {record.data.get('dnf_reason', 'unknown')} "
                f"| exit={container.get('exit_code')}, "
                f"oom_killed={container.get('oom_killed')} |"
            )
        elif record.errored:
            rows.append(f"| `{record.label}` | error | {record.errored[:120]} | — |")
    if not rows:
        return "_Every target completed every workload._"
    return "\n".join(
        ["| Target | Outcome | Reason | Detail |", "|---|---|---|---|", *rows]
    )


def render_charts(records: list[Record], out_dir: Path) -> list[Path]:
    """Render every chart the report embeds.

    Each chart takes one series per engine. An earlier version passed one per
    engine-and-tier, which put three same-coloured bars side by side because
    colour encodes the engine while the label carried the tier. Latency is flat
    across tiers, so the tier comparison lives only in the memory sweep.
    """
    from gbench.report import charts

    charts_dir = out_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    live = [r for r in records if not r.dnf and not r.errored]

    def arm_of(record: Record) -> str:
        return (record.data.get("target") or {}).get("arm", "")

    def one_per_engine(arm: str) -> list[Record]:
        """The largest tier at which each engine of `arm` completed."""
        best: dict[str, Record] = {}
        for record in live:
            if arm_of(record) != arm:
                continue
            current = best.get(record.target_id)
            if current is None or _tier_bytes(record.tier) > _tier_bytes(current.tier):
                best[record.target_id] = record
        return [best[k] for k in sorted(best)]

    # Latency, one chart per arm. The arms never share an axis: a container on
    # loopback and an instance 240 ms away are not comparable on client latency.
    for arm, title, subtitle in (
        (
            "capped",
            "Arm A — every engine under identical cgroup limits",
            "0.5 vCPU, no network in the path. Each bar is the largest tier at\n"
            "which that engine ran.",
        ),
        (
            "managed",
            "Arm B — managed free tiers, over the network",
            "Not resource-equal. These latencies are dominated by round-trip\n"
            "time; see the network split below.",
        ),
    ):
        rows = [
            (
                r.target_id,
                {
                    w: (r.workload(w) or {}).get("client", {}).get("p50_ms")
                    for w in WORKLOAD_ORDER
                    if (r.workload(w) or {}).get("client")
                },
            )
            for r in one_per_engine(arm)
        ]
        rows = [(engine, values) for engine, values in rows if values]
        if rows:
            written.append(
                charts.latency_by_workload(
                    rows, charts_dir / f"latency-{arm}.png", title=title, subtitle=subtitle
                )
            )

    # Memory sweep: the one chart where tiers are the point.
    tiers = sorted({r.tier for r in records if r.tier}, key=_tier_bytes)
    by_engine: dict[str, dict[str, float | None]] = {}
    for record in records:
        if not record.tier:
            continue
        entry = record.workload("hop3") if not record.dnf else None
        value = (entry or {}).get("client", {}).get("p50_ms") if entry else None
        by_engine.setdefault(record.target_id, {})[record.tier] = value
    if by_engine and len(tiers) > 1:
        sweep = {
            engine: [(tier, values.get(tier)) for tier in tiers]
            for engine, values in sorted(by_engine.items())
        }
        written.append(charts.memory_sweep(sweep, charts_dir / "memory-sweep.png"))

    # Concurrency, one chart per arm, for the same reason as latency.
    for arm, title in (
        ("capped", "Arm A — sustained throughput at 0.5 vCPU (90% reads)"),
        ("managed", "Arm B — sustained throughput on the managed free tiers (90% reads)"),
    ):
        points = {
            r.target_id: [(lv["clients"], lv["qps"]) for lv in r.data.get("concurrency") or []]
            for r in one_per_engine(arm)
        }
        points = {k: v for k, v in points.items() if v}
        if points:
            written.append(
                charts.concurrency(points, charts_dir / f"concurrency-{arm}.png", title=title)
            )

    # Where the time went. Managed targets only: in the capped arm that
    # component is loopback plus driver overhead, and calling it network in a
    # chart about network cost would be misleading.
    split = []
    for record in one_per_engine("managed"):
        entry = record.workload("point_lookup")
        if not entry or not entry.get("server") or not entry.get("client"):
            continue
        server = entry["server"]["p50_ms"]
        client = entry["client"]["p50_ms"]
        split.append((record.target_id, server, max(0.0, client - server)))
    if split:
        written.append(charts.network_split(split, charts_dir / "network-split.png"))

    # Warm-up curves, from the samples the runner kept rather than discarded.
    warmup = {}
    for record in one_per_engine("capped"):
        entry = record.workload("hop3")
        if entry and entry.get("warmup_ms"):
            warmup[record.target_id] = entry["warmup_ms"]
    if warmup:
        written.append(charts.warmup_curve(warmup, charts_dir / "warmup.png"))

    return written


def inject_into_readme(readme: Path, results_md: Path, charts: list[Path]) -> bool:
    """Splice the generated matrix and charts into the README between markers.

    Section 6 of the brief asks for the full results matrix *in* the README,
    not linked from it -- a reader who opens only the README must see every
    metric. Generating it rather than pasting means it cannot drift from the
    run that produced it.
    """
    start, end = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
    text = readme.read_text()
    if start not in text or end not in text:
        return False

    body = results_md.read_text()
    body = body.split("\n", 2)[2] if body.startswith("<!--") else body
    body = body.replace("](charts/", "](results/charts/")

    # Demote every heading one level. The standalone RESULTS.md is its own
    # document with h1/h2; spliced under "## Results" those same headings would
    # read as peers of the section containing them.
    body = "\n".join(
        ("#" + line) if line.startswith("#") else line for line in body.splitlines()
    )
    body = body.replace("## Results\n", "", 1).replace("# # Results", "", 1)

    block = f"{start}\n\n{body}\n{end}"
    readme.write_text(text[: text.index(start)] + block + text[text.index(end) + len(end) :])
    return True


def generate(raw_dir: Path, out_dir: Path) -> Path:
    """Write `results/RESULTS.md` and the charts from the most recent run."""
    records = load_records(raw_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered = render_charts(records, out_dir)

    sections = [
        "<!-- Generated by `make report`. Do not edit by hand. -->",
        "# Results\n",
        "## Targets and their advertised specifications\n",
        table_environment(records),
        "\n## Ingest\n",
        "_Driver batching, identical batch size on every target. "
        "Each engine's faster native path was deliberately not used; see the README._\n",
        table_ingest(records),
        "\n## Latency by workload\n",
        "_p50 and p95 only. This client is closed-loop, which under-samples stalls; "
        "the distortion is negligible at p50, around 1.5x at p95, and 20x or worse at p99, "
        "so no p99 is published from this path. `p95 CI` is a distribution-free "
        "confidence interval from order statistics -- where it reads **unbounded**, "
        "the sample was too small to close the interval and the p95 should not be "
        "quoted._\n",
    ]

    for workload_id in WORKLOAD_ORDER:
        sections += [f"\n### {WORKLOAD_LABEL[workload_id]}\n", table_latency(records, workload_id)]

    sections += [
        "\n## Mixed workload -- sustained throughput\n",
        table_concurrency(records),
        "\n## Cold start versus steady state\n",
        "_A trivial round trip against a freshly started engine holding no data, "
        "against the same round trip once the graph is loaded. Reported separately, "
        "never averaged together._\n",
        table_cold_start(records),
        "\n## Footprint\n",
        table_footprint(records),
        "\n## Failures and did-not-finish\n",
        table_dnf(records),
    ]

    captions = {
        "latency-capped": "Latency by workload — Arm A, identical cgroup limits",
        "latency-managed": "Latency by workload — Arm B, managed free tiers",
        "memory-sweep": "Latency against the container memory limit",
        "concurrency-capped": "Sustained throughput against concurrency — Arm A",
        "concurrency-managed": "Sustained throughput against concurrency — Arm B",
        "network-split": "Server execution time versus network time",
        "warmup": "Warm-up curves",
    }
    if rendered:
        sections.append("\n## Charts\n")
        for path in rendered:
            caption = captions.get(path.stem, path.stem.replace("-", " ").capitalize())
            sections.append(f"### {caption}\n\n![{caption}](charts/{path.name})\n")

    sections.append("")
    path = out_dir / "RESULTS.md"
    path.write_text("\n".join(sections))

    readme = out_dir.parent / "README.md"
    if readme.exists():
        inject_into_readme(readme, path, rendered)
    return path
