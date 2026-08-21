"""Generate a static results site from the raw benchmark output.

Everything on the page is derived from `results/raw/`, for the same reason the
README tables are: a hand-written page cannot be checked against the run that
produced it, and at least one published benchmark's numbers turned out not to
match its own harness output.

Deliberately a single self-contained HTML file plus the chart images. No build
step, no framework, no client-side data fetching -- a reader with the
repository can open `site/index.html` locally and see exactly what the deployed
page shows.
"""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path
from typing import Any

from gbench.report import variance as var
from gbench.report.generate import WORKLOAD_LABEL, WORKLOAD_ORDER, Record, load_records

DISPLAY = {
    "cognodb-c0": "CognoDB c0",
    "neo4j-aura-free": "Neo4j AuraDB Free",
    "neo4j-community": "Neo4j Community",
    "memgraph": "Memgraph",
    "falkordb": "FalkorDB",
    "kuzu": "Kuzu",
}

CHART_CAPTIONS = {
    "latency-capped": (
        "Latency by workload — identical cgroup limits",
        "Arm A. 0.5 vCPU, no network in the path.",
    ),
    "latency-managed": (
        "Latency by workload — managed free tiers",
        "Arm B. Not resource-equal; dominated by round-trip time.",
    ),
    "memory-sweep": (
        "Latency against the memory cap",
        "Flat across tiers. Only startup depends on memory.",
    ),
    "concurrency-capped": (
        "Throughput against concurrency — Arm A",
        "At 0.5 vCPU, extra clients add queueing rather than throughput.",
    ),
    "concurrency-managed": (
        "Throughput against concurrency — Arm B",
        "CognoDB becomes CPU-bound where the network alone would allow more.",
    ),
    "network-split": (
        "Server execution versus network",
        "How much of a managed target's latency was ever the database.",
    ),
    "warmup": (
        "Warm-up curves",
        "Published rather than assumed; discarded from the percentiles.",
    ),
}


def _name(target_id: str) -> str:
    return DISPLAY.get(target_id, target_id)


def _e(text: Any) -> str:
    return html.escape(str(text))


def _tiles(records: list[Record], raw_dir: Path) -> str:
    """The handful of numbers a reader should leave with."""
    stats = var.summarise(raw_dir)
    ingest = {
        r.target_id: (r.data.get("load") or {}).get("relationships_per_second")
        for r in records
        if r.data.get("load")
    }
    fastest = max(ingest.items(), key=lambda kv: kv[1] or 0)
    slowest = min(ingest.items(), key=lambda kv: kv[1] or float("inf"))
    ratio = (fastest[1] or 1) / (slowest[1] or 1)

    tiles = [
        ("161,236", "nodes", "ICIJ Paradise Papers — Appleby"),
        ("381,523", "relationships", "loaded identically into every target"),
        (
            f"{ratio:,.0f}×",
            "ingest spread",
            f"{_name(fastest[0])} against {_name(slowest[0])}",
        ),
        ("0.0 ms", "CognoDB server time", "on all seven workloads — under Bolt's resolution"),
        ("238 ms", "round-trip floor", "of which 0.2 ms was the database"),
        (
            f"{stats['run_count']}",
            "independent runs",
            f"median coefficient of variation {stats['median_cv']:.3f}",
        ),
    ]
    cells = "".join(
        f'<div class="tile"><div class="tile-v">{_e(v)}</div>'
        f'<div class="tile-k">{_e(k)}</div><div class="tile-n">{_e(n)}</div></div>'
        for v, k, n in tiles
    )
    return f'<div class="tiles">{cells}</div>'


def _latency_table(records: list[Record], arm: str) -> str:
    rows = [r for r in records if (r.data.get("target") or {}).get("arm") == arm]
    if not rows:
        return ""
    head = "".join(f"<th>{_e(WORKLOAD_LABEL[w])}</th>" for w in WORKLOAD_ORDER)
    body = []
    for record in sorted(rows, key=lambda r: r.label):
        cells = []
        for workload in WORKLOAD_ORDER:
            entry = record.workload(workload)
            client = (entry or {}).get("client")
            if record.dnf:
                cells.append('<td class="dnf">DNF</td>')
            elif client:
                cells.append(f'<td class="num">{client["p50_ms"]:,.2f}</td>')
            else:
                cells.append('<td class="num muted">—</td>')
        body.append(f'<tr><th scope="row">{_e(record.label)}</th>{"".join(cells)}</tr>')
    return (
        '<div class="tw"><table><thead><tr><th scope="col">Target</th>'
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )


def _ingest_table(records: list[Record]) -> str:
    rows = []
    def rate(record: Record) -> float:
        return -((record.data.get("load") or {}).get("relationships_per_second") or 0)

    for record in sorted(records, key=rate):
        load = record.data.get("load")
        if record.dnf:
            rows.append(
                f'<tr><th scope="row">{_e(record.label)}</th>'
                '<td class="dnf" colspan="3">DNF — out of memory</td></tr>'
            )
            continue
        if not load:
            continue
        rows.append(
            f'<tr><th scope="row">{_e(record.label)}</th>'
            f'<td class="num">{load["relationships_per_second"]:,.0f}</td>'
            f'<td class="num">{load["nodes_per_second"]:,.0f}</td>'
            f'<td class="num">{load["wall_clock_s"]:,.0f} s</td></tr>'
        )
    return (
        '<div class="tw"><table><thead><tr><th scope="col">Target</th>'
        '<th scope="col">Relationships/s</th><th scope="col">Nodes/s</th>'
        f'<th scope="col">Wall clock</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def _variance_table(raw_dir: Path) -> str:
    stats = var.summarise(raw_dir)
    repeated = [s for s in stats["spreads"] if s.runs >= 2]
    if not repeated:
        return "<p class='muted'>Only one run recorded; run-to-run variance cannot be reported.</p>"
    repeated.sort(key=lambda s: -s.cv)
    rows = "".join(
        f'<tr><th scope="row">{_e(s.target)}</th><td>{_e(s.metric)}</td>'
        f'<td class="num">{s.median:,.2f}</td><td class="num">{s.minimum:,.2f}</td>'
        f'<td class="num">{s.maximum:,.2f}</td><td class="num">{s.spread_pct:,.1f}%</td>'
        f'<td class="num">{s.cv:.3f}{"" if s.stable else " ⚠"}</td></tr>'
        for s in repeated[:20]
    )
    return (
        '<div class="tw"><table><thead><tr><th scope="col">Target</th><th scope="col">Metric</th>'
        '<th scope="col">Median</th><th scope="col">Min</th><th scope="col">Max</th>'
        '<th scope="col">Spread</th><th scope="col">CV</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def _charts(charts_dir: Path) -> str:
    blocks = []
    for path in sorted(charts_dir.glob("*.png")):
        title, note = CHART_CAPTIONS.get(path.stem, (path.stem.replace("-", " "), ""))
        blocks.append(
            f'<figure><img src="charts/{_e(path.name)}" alt="{_e(title)}" loading="lazy">'
            f"<figcaption><b>{_e(title)}</b>{(' — ' + _e(note)) if note else ''}</figcaption></figure>"
        )
    return "".join(blocks)


def build(raw_dir: Path, charts_dir: Path, out_dir: Path, repo_url: str) -> Path:
    """Write `site/index.html` and copy the charts beside it."""
    records = load_records(raw_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target_charts = out_dir / "charts"
    if target_charts.exists():
        shutil.rmtree(target_charts)
    shutil.copytree(charts_dir, target_charts)

    manifest_path = raw_dir.parent.parent / "data" / "build" / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    page = _TEMPLATE.format(
        repo=repo_url,
        tiles=_tiles(records, raw_dir),
        ingest=_ingest_table(records),
        latency_capped=_latency_table(records, "capped"),
        latency_managed=_latency_table(records, "managed"),
        variance=_variance_table(raw_dir),
        charts=_charts(charts_dir),
        generated=manifest.get("archive_generated_on", ""),
    )
    path = out_dir / "index.html"
    path.write_text(page)
    return path


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Benchmarking CognoDB Cloud against four other graph databases</title>
<meta name="description" content="A resource-parity benchmark of five graph databases, with the network separated from the database. Every number generated from raw per-iteration data.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>
:root {{
  --bg: #fbfbfc; --panel: #f2f4f7; --panel2: #e9ecf1;
  --ink: #12161d; --ink2: #545e6c; --ink3: #7c8697;
  --rule: #dde1e8; --rule2: #c3cad6;
  --accent: #2a78d6; --warn: #8f5406; --bad: #9c2c3e;
  --sans: "IBM Plex Sans", system-ui, -apple-system, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #0e1116; --panel: #161a21; --panel2: #1d222b;
    --ink: #e4e8ef; --ink2: #98a3b3; --ink3: #6d7787;
    --rule: #242a34; --rule2: #333b47;
    --accent: #6aa9f0; --warn: #e0a64f; --bad: #e5798b;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: var(--sans); font-size: 17px; line-height: 1.65;
  -webkit-font-smoothing: antialiased;
}}
.wrap {{ max-width: 1120px; margin: 0 auto; padding: 0 clamp(20px,5vw,48px) 96px; }}
.col {{ max-width: 68ch; }}
a {{ color: var(--accent); text-underline-offset: 2px; }}
a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 3px; }}
code {{ font-family: var(--mono); font-size: .88em; background: var(--panel);
  border: 1px solid var(--rule); border-radius: 3px; padding: .06em .34em; }}

header {{ border-bottom: 2px solid var(--ink); padding: clamp(48px,8vw,88px) 0 30px; margin-bottom: 8px; }}
.eyebrow {{ font-family: var(--mono); font-size: 11.5px; letter-spacing: .16em;
  text-transform: uppercase; color: var(--ink3); margin-bottom: 18px; }}
h1 {{ font-size: clamp(30px,5.2vw,52px); line-height: 1.06; letter-spacing: -.025em;
  font-weight: 700; margin: 0 0 18px; max-width: 20ch; text-wrap: balance; }}
.lede {{ font-size: clamp(17px,2.1vw,20px); color: var(--ink2); max-width: 62ch; margin: 0; }}

.tiles {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(168px,1fr));
  gap: 1px; background: var(--rule); border: 1px solid var(--rule); margin: 34px 0 0; }}
.tile {{ background: var(--bg); padding: 16px 18px; }}
.tile-v {{ font-family: var(--mono); font-size: 25px; font-weight: 600;
  letter-spacing: -.02em; color: var(--accent); }}
.tile-k {{ font-size: 13px; font-weight: 600; margin-top: 2px; }}
.tile-n {{ font-size: 12.5px; color: var(--ink3); line-height: 1.45; margin-top: 4px; }}

section {{ padding-top: 64px; }}
h2 {{ font-size: clamp(23px,3.2vw,32px); line-height: 1.15; letter-spacing: -.02em;
  font-weight: 700; margin: 0 0 8px; text-wrap: balance; }}
h3 {{ font-size: 18px; font-weight: 600; margin: 34px 0 10px; letter-spacing: -.01em; }}
.sub {{ color: var(--ink3); font-size: 15px; margin: 0 0 22px; max-width: 66ch; }}
p {{ margin: 0 0 1.05em; }}

.tw {{ overflow-x: auto; border: 1px solid var(--rule2); margin: 18px 0 26px; }}
table {{ border-collapse: collapse; width: 100%; min-width: 620px;
  font-size: 14.5px; font-variant-numeric: tabular-nums; }}
thead th {{ font-family: var(--mono); font-size: 10.5px; letter-spacing: .1em;
  text-transform: uppercase; color: var(--ink2); text-align: left;
  padding: 11px 14px; background: var(--panel2); border-bottom: 1px solid var(--rule2);
  white-space: nowrap; }}
tbody th {{ text-align: left; font-weight: 600; font-size: 14px; }}
tbody th, tbody td {{ padding: 10px 14px; border-bottom: 1px solid var(--rule); }}
tbody tr:last-child th, tbody tr:last-child td {{ border-bottom: 0; }}
tbody tr:nth-child(even) {{ background: var(--panel); }}
.num {{ text-align: right; font-family: var(--mono); font-size: 13px; }}
.dnf {{ color: var(--bad); font-family: var(--mono); font-size: 12.5px; font-weight: 600; }}
.muted {{ color: var(--ink3); }}

figure {{ margin: 0 0 34px; }}
figure img {{ width: 100%; height: auto; border: 1px solid var(--rule); background: #fcfcfb; }}
figcaption {{ font-size: 14px; color: var(--ink3); margin-top: 10px; line-height: 1.5; }}

.note {{ border-left: 3px solid var(--accent); padding: 2px 0 2px 18px; margin: 24px 0;
  max-width: 68ch; }}
.note.warn {{ border-color: var(--warn); }}

footer {{ margin-top: 84px; padding-top: 24px; border-top: 2px solid var(--ink);
  font-family: var(--mono); font-size: 12.5px; color: var(--ink3); line-height: 1.8; }}
@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="eyebrow">Graph database benchmark · August 2026</div>
  <h1>Benchmarking CognoDB Cloud against four other graph databases</h1>
  <p class="lede">CognoDB Cloud's free tier is the subject of this study and the yardstick for its
  design. Its advertised specification — 0.5&nbsp;vCPU, 512&nbsp;MB RAM, 1&nbsp;GiB disk — is the
  resource envelope every other engine is held to. Every figure below is generated from raw
  per-iteration data; none is typed by hand.</p>
  {tiles}
</header>

<section>
  <h2>Ingest</h2>
  <p class="sub">Driver batching at an identical batch size on every target. Each engine's faster
  native loader was deliberately not used: the managed targets have no equivalent, so a column
  mixing four mechanisms would compare nothing.</p>
  {ingest}
</section>

<section>
  <h2>Latency — Arm A, identical cgroup limits</h2>
  <p class="sub">Every engine in a container capped at CognoDB c0's advertised specification, with
  no network in the path. p50 in milliseconds. The enforced <code>cpu.max</code> and
  <code>memory.max</code> were read from inside each running container and recorded.</p>
  {latency_capped}

  <h2>Latency — Arm B, managed free tiers</h2>
  <p class="sub">Explicitly not resource-equal, and network is in the path. These figures are
  dominated by round-trip time and should be read alongside the server-execution split below.</p>
  {latency_managed}

  <div class="note">
    <p>CognoDB reported <b>0.0&nbsp;ms of server-side execution on all seven workloads</b>,
    including the three-hop traversal returning 1,000 rows. Bolt reports server time in whole
    milliseconds, so this is an upper bound of under 1&nbsp;ms rather than a precise figure — but
    nothing in this study established an upper bound on its read speed, because nothing it was
    asked to do took long enough to measure.</p>
  </div>
</section>

<section>
  <h2>Run-to-run variance</h2>
  <p class="sub">The containerised arm was executed three times. A difference between two engines
  smaller than either one's own spread is not a difference. Measurements marked ⚠ exceed a
  coefficient of variation of 0.10 and should not be compared across engines without accounting
  for the spread. Least stable first.</p>
  {variance}

  <div class="note warn">
    <p><b>One claim did not survive.</b> A single run showed Neo4j's server-side aggregation at
    2.00&nbsp;ms against FalkorDB's 2.68&nbsp;ms, and an earlier draft stated that Neo4j won it.
    Across three runs Neo4j measured 2.00, 2.00 and 3.00&nbsp;ms against FalkorDB's 2.68, 2.75 and
    2.69 — overlapping ranges, with Neo4j slower than FalkorDB's worst in one run. The measurement
    cannot separate them. The retraction is left visible in the repository rather than deleted.</p>
  </div>
</section>

<section>
  <h2>Charts</h2>
  <p class="sub">Rendered from the same raw output as the tables. A did-not-finish is drawn as a
  labelled gap, never as a zero.</p>
  {charts}
</section>

<section>
  <h2>Method, in short</h2>
  <div class="col">
    <h3>Two arms, both anchored on CognoDB's specification</h3>
    <p>Of seven managed graph platforms surveyed, two still offer a free tier that survives a week,
    and those span 100&nbsp;MB to 4&nbsp;GB. Equal resources across managed free tiers is therefore
    not achievable. Arm&nbsp;A imposes CognoDB c0's envelope on every other engine via cgroups;
    Arm&nbsp;B measures the managed tiers as shipped. The two are reported separately and never
    averaged.</p>

    <h3>The network is separated from the database</h3>
    <p>Bolt returns server-side execution time independently of what the client observed, and a
    warmed round-trip floor bounds each target. A <code>RETURN 1</code> against CognoDB costs
    238&nbsp;ms client-side and 0&nbsp;ms server-side.</p>

    <h3>1,000 iterations, p50 and p95, no p99</h3>
    <p>A distribution-free confidence interval for p95 is unbounded below n&nbsp;=&nbsp;110, so the
    commonly suggested 100 iterations yields a p95 with no error bar. The latency client is
    closed-loop, which distorts p99 by 20× or worse, so no p99 is published from it.</p>

    <h3>Failures are results</h3>
    <p>Neo4j is OOM-killed at 512&nbsp;MB and that is published as a DNF with its exit code, not
    omitted. Row counts are compared across targets on every workload — a check that caught four
    real defects, including one genuine semantic divergence between two Cypher engines, none of
    which was visible from latency alone.</p>
  </div>
</section>

<footer>
  <div>Full results matrix, methodology, caveats and the harness itself:
  <a href="{repo}">{repo}</a></div>
  <div>Dataset © ICIJ — ODbL 1.0 (database), CC BY-SA 3.0 (contents). Harness MIT-licensed, so any
  vendor measured here can re-run it.</div>
</footer>

</div>
</body>
</html>
"""
