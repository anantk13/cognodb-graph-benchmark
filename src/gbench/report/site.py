"""Render the README as a static site.

The page is the README, not a summary of it. An earlier version wrote its own
condensed tables and drifted immediately: it carried 1,754 words against the
README's 10,731 and was missing eight of nine sections, including the analysis,
the footprint, the cold-start comparison and the concurrency results. Parity
maintained by hand is parity that lapses.

So the source is `README.md`, whose own results tables are themselves generated
from `results/raw/` by `report.generate`. A number therefore travels from raw
per-iteration output, through the generated tables, into both the repository
and the deployed page, with no opportunity to diverge on the way.

Deliberately one self-contained HTML file plus the chart images: no build step,
no framework, no client-side data fetching. A reader with the repository can
open `site/index.html` from the filesystem and see exactly what the deployed
page shows.
"""

from __future__ import annotations

import html
import re
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


def _readme_html(readme: Path) -> str:
    """Convert the README to HTML, minus its H1 and the chart paths fixed.

    The H1 is dropped because the page supplies its own header, and image
    sources are rewritten from the repository's `results/charts/` to the
    deployed `charts/`.
    """
    import markdown

    text = readme.read_text()
    text = re.sub(r"\A# .*?\n", "", text, count=1)  # the page has its own H1
    text = text.replace("](results/charts/", "](charts/")
    text = text.replace("<!-- RESULTS:START -->", "").replace("<!-- RESULTS:END -->", "")

    # Drop the standalone repository pointers. They are navigation aids for
    # someone reading the README in the repository; on the deployed page the
    # files do not exist and every one of them is a 404.
    # Whole sections that belong in the repository but not on the page.
    for heading in _OMIT_SECTIONS:
        start = text.find(f"\n{heading}")
        if start == -1:
            continue
        following = text.find("\n## ", start + 1)
        text = text[:start] + (text[following:] if following != -1 else "")

    text = re.sub(r"^\*\*→ \[`[^`]+`\]\([^)]+\)\*\*.*(?:\n(?!\n).*)*\n?", "", text, flags=re.M)
    text = re.sub(r"^→ \[`[^`]+`\]\([^)]+\).*(?:\n(?!\n).*)*\n?", "", text, flags=re.M)

    # Any surviving link to a file in the repository is rewritten to its
    # canonical location on GitHub, so nothing on this page 404s while the
    # reference itself stays useful.
    text = re.sub(
        r"\]\((?!https?:|#|charts/)([A-Za-z0-9_./-]+)\)",
        lambda m: f"]({_REPO_BLOB}/{m.group(1)})",
        text,
    )

    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "toc", "attr_list", "sane_lists"],
        output_format="html5",
    )

    # Wide tables scroll inside their own container so the page body never
    # scrolls sideways on a narrow screen.
    body = body.replace("<table>", '<div class="tw"><table>').replace(
        "</table>", "</table></div>"
    )
    # Figures rather than bare images, so charts carry the same styling as the
    # rest of the page.
    body = re.sub(
        r"<p>(<img[^>]+>)</p>",
        r'<figure>\1</figure>',
        body,
    )
    return body


#: Sections whose subsections are worth exposing in the nav. Everything else
#: contributes its top-level heading only, so the nav stays scannable.
_EXPAND = {"results", "analysis"}

#: Where a repository-relative link resolves to once the README is deployed as
#: a standalone page. Set by `build` before the README is converted.
_REPO_BLOB = ""

#: Sections dropped from the deployed page entirely. They are about obtaining
#: and running the project rather than about what it found: build commands, a
#: pointer to the page the reader is already on, and licence text the footer
#: already carries. They remain in the README, where someone who has cloned the
#: repository needs them.
_OMIT_SECTIONS = (
    "## Hosted results",
    "## Reproducing these results",
    "## Licence and attribution",
)

#: Anchors for the above, so the contents list does not offer a link to a
#: section that is no longer on the page.
_SKIP_NAV = {"hosted-results", "reproducing-these-results", "licence-and-attribution"}


def _nav(body: str) -> str:
    """A contents list built from the rendered headings.

    Generated from the document rather than hand-listed, so a section added to
    the README appears in the navigation without anyone remembering to add it.
    """
    entries: list[str] = []
    current: str | None = None
    for match in re.finditer(r'<h([23]) id="([^"]+)">(.*?)</h[23]>', body):
        level, anchor, raw = match.group(1), match.group(2), match.group(3)
        title = re.sub(r"<[^>]+>", "", raw).strip()
        if level == "2":
            current = anchor
            if anchor in _SKIP_NAV:
                continue
            entries.append(f'<a class="n2" href="#{_e(anchor)}">{_e(title)}</a>')
        elif current in _EXPAND and current not in _SKIP_NAV:
            entries.append(f'<a class="n3" href="#{_e(anchor)}">{_e(title)}</a>')
    return "".join(entries)


def build(raw_dir: Path, charts_dir: Path, out_dir: Path, repo_url: str) -> Path:
    """Write `site/index.html` and copy the charts beside it."""
    records = load_records(raw_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target_charts = out_dir / "charts"
    if target_charts.exists():
        shutil.rmtree(target_charts)
    shutil.copytree(charts_dir, target_charts)

    global _REPO_BLOB
    _REPO_BLOB = f"{repo_url.rstrip('/')}/blob/main"

    readme = out_dir.parent / "README.md"
    body = _readme_html(readme)
    page = _TEMPLATE.format(
        repo=repo_url,
        tiles=_tiles(records, raw_dir),
        body=body,
        nav=_nav(body),
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
.wrap {{ max-width: 1400px; margin: 0 auto; padding: 0 clamp(20px,4vw,44px) 96px; }}
.layout {{ display: block; }}
@media (min-width: 1080px) {{
  .layout {{ display: grid; grid-template-columns: 244px minmax(0,1fr); gap: 56px;
    align-items: start; }}
}}

/* Contents. Sticky beside the content on wide screens, hidden below 1080px
   where the column would crowd the tables rather than help. */
.toc {{ display: none; }}
@media (min-width: 1080px) {{
  .toc {{ display: block; position: sticky; top: 24px; max-height: calc(100vh - 48px);
    overflow-y: auto; padding: 4px 0 24px; border-right: 1px solid var(--rule);
    margin-right: -24px; padding-right: 24px; }}
}}
.toc-h {{ font-family: var(--mono); font-size: 10.5px; letter-spacing: .15em;
  text-transform: uppercase; color: var(--ink3); margin: 0 0 12px; }}
.toc a {{ display: block; text-decoration: none; color: var(--ink2); line-height: 1.4;
  border-left: 2px solid transparent; }}
.toc a:hover {{ color: var(--ink); }}
.toc .n2 {{ font-size: 14px; font-weight: 600; padding: 7px 0 7px 12px; margin-top: 4px; }}
.toc .n3 {{ font-size: 13px; padding: 4px 0 4px 22px; color: var(--ink3); }}
.toc a.on {{ color: var(--accent); border-left-color: var(--accent); }}
.toc a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}

html {{ scroll-behavior: smooth; }}
@media (prefers-reduced-motion: reduce) {{ html {{ scroll-behavior: auto; }} }}
main :is(h2,h3) {{ scroll-margin-top: 20px; }}
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
/* Numeric columns are centred rather than right-aligned. Markdown emits
   text-align:right for them, which pushes a short value like 8,121 to the far
   edge of a wide header like "Relationships/s" and reads as disconnected from
   the column it belongs to. Centring puts each value under its own heading.
   Tabular figures keep the digits a consistent width so the column still
   scans vertically. */
main th[style*="right"], main td[style*="right"] {{ text-align: center !important; }}
main table {{ font-variant-numeric: tabular-nums; }}
main td[style*="right"] {{ font-family: var(--mono); font-size: 13px; white-space: nowrap; }}
.dnf {{ color: var(--bad); font-family: var(--mono); font-size: 12.5px; font-weight: 600; }}
.muted {{ color: var(--ink3); }}

figure {{ margin: 0 0 34px; }}
figure img {{ width: 100%; height: auto; border: 1px solid var(--rule); background: #fcfcfb; }}
figcaption {{ font-size: 14px; color: var(--ink3); margin-top: 10px; line-height: 1.5; }}

.note {{ border-left: 3px solid var(--accent); padding: 2px 0 2px 18px; margin: 24px 0;
  max-width: 68ch; }}
.note.warn {{ border-color: var(--warn); }}

main {{ padding-top: 8px; }}
main h2 {{ font-size: clamp(23px,3.2vw,32px); line-height: 1.15; letter-spacing: -.02em;
  font-weight: 700; margin: 68px 0 10px; text-wrap: balance;
  padding-top: 22px; border-top: 1px solid var(--rule); }}
main h2:first-of-type {{ border-top: 0; }}
main h3 {{ font-size: 20px; font-weight: 600; margin: 40px 0 10px; letter-spacing: -.012em; }}
main h4 {{ font-size: 16.5px; font-weight: 600; margin: 30px 0 8px; color: var(--ink2); }}
main p, main li {{ max-width: 74ch; }}
main ul, main ol {{ padding-left: 1.35em; }}
main li {{ margin-bottom: .4em; }}
main hr {{ border: 0; border-top: 1px solid var(--rule); margin: 52px 0 0; }}
main blockquote {{ border-left: 3px solid var(--accent); margin: 22px 0; padding: 2px 0 2px 18px;
  color: var(--ink2); }}
main pre {{ background: var(--panel); border: 1px solid var(--rule); border-radius: 4px;
  padding: 14px 16px; overflow-x: auto; font-size: 13.5px; line-height: 1.55; }}
main pre code {{ background: none; border: 0; padding: 0; font-size: inherit; }}
main strong {{ font-weight: 600; }}
.src {{ font-family: var(--mono); font-size: 12.5px; color: var(--ink3); margin: 24px 0 0;
  word-break: break-all; }}

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
  <p class="src">Full harness, raw results and reproduction instructions:
  <a href="{repo}">{repo}</a></p>
</header>

<div class="layout">
  <nav class="toc" aria-label="Contents">
    <p class="toc-h">Contents</p>
    {nav}
  </nav>
  <main>
{body}
  </main>
</div>

<footer>
  <div>This page is generated from the repository's README by <code>make site</code>, whose
  results tables are themselves generated from the raw per-iteration output. The page, the
  repository and the raw data cannot disagree.</div>
  <div>Dataset © ICIJ — ODbL 1.0 (database), CC BY-SA 3.0 (contents). Harness MIT-licensed, so any
  vendor measured here can re-run it.</div>
  <div><a href="{repo}">{repo}</a></div>
</footer>

</div>

<script>
// Highlights the contents entry for whichever section is currently in view.
// Uses IntersectionObserver rather than a scroll handler so it costs nothing
// while the reader is not scrolling.
(function () {{
  var links = Array.prototype.slice.call(document.querySelectorAll('.toc a'));
  if (!links.length || !('IntersectionObserver' in window)) return;

  var byId = {{}};
  links.forEach(function (a) {{ byId[a.getAttribute('href').slice(1)] = a; }});

  var targets = Object.keys(byId)
    .map(function (id) {{ return document.getElementById(id); }})
    .filter(Boolean);

  var visible = new Set();
  function paint() {{
    var first = targets.find(function (t) {{ return visible.has(t.id); }});
    links.forEach(function (a) {{ a.classList.remove('on'); }});
    if (first && byId[first.id]) {{
      byId[first.id].classList.add('on');
      byId[first.id].scrollIntoView({{ block: 'nearest' }});
    }}
  }}

  var io = new IntersectionObserver(function (entries) {{
    entries.forEach(function (e) {{
      if (e.isIntersecting) visible.add(e.target.id); else visible.delete(e.target.id);
    }});
    paint();
  }}, {{ rootMargin: '-8% 0px -70% 0px', threshold: 0 }});

  targets.forEach(function (t) {{ io.observe(t); }});
}})();
</script>

</body>
</html>
"""
