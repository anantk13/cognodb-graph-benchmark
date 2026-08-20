"""Reduce the ICIJ Offshore Leaks release to one self-consistent subgraph.

The published archive is the whole database -- five investigations, 5.36 million
rows -- which is far too large for a 1 GB free tier. It has to be cut down, and
*how* it is cut down is a methodology decision, not a detail.

This module takes a **natural subset**: every row the source itself labels
`Paradise Papers - Appleby`. That is a slice the data already defines, not one
this harness invented. The alternative -- a seeded random subsample -- would
have been defensible but weaker, because a reader then has to trust that the
sampler did not accidentally pick an easy graph. A named source is checkable:
anyone can re-run the filter and get the identical rows.

Appleby is 163,267 nodes and 390,966 relationships before dangling edges are
removed, which sits inside the 100k-500k range the assignment suggests.

One rule is applied beyond the source filter: a relationship is kept only if
*both* endpoints survive. Cross-source edges pointing at nodes from Panama
Papers or Bahamas Leaks would otherwise load as dangling references, and every
engine handles those differently -- some create phantom nodes, some error, some
silently drop. Different graphs on different engines is precisely what a
benchmark must not have.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

csv.field_size_limit(10**9)

SOURCE_ID = "Paradise Papers - Appleby"

#: Node CSV -> label. The archive ships one file per node type; `nodes-others`
#: is excluded because its rows are a residual category with no consistent
#: schema, and a label whose members share no properties cannot support the
#: filtered-lookup or aggregation workloads.
NODE_FILES: dict[str, str] = {
    "nodes-entities.csv": "Entity",
    "nodes-officers.csv": "Officer",
    "nodes-addresses.csv": "Address",
    "nodes-intermediaries.csv": "Intermediary",
}

#: Properties carried through to the benchmark graph. Kept deliberately small:
#: every property is loaded into every engine, and unused columns inflate the
#: ingest measurement without exercising anything. Each one below is read by at
#: least one workload.
NODE_PROPERTIES = (
    "name",  # point lookup
    "jurisdiction",  # indexed filtered lookup, group-by
    "countries",  # group-by
    "country_codes",
    "status",  # filtered lookup
    "incorporation_date",
)


@dataclass
class Manifest:
    """What was actually built, recorded so a re-run can be checked against it.

    The upstream URL ends in `LATEST`, so the archive behind it changes without
    notice. Pinning the release date and the archive checksum is what makes
    "same dataset on every platform" verifiable months later rather than merely
    asserted.
    """

    source_id: str = SOURCE_ID
    archive_sha256: str = ""
    archive_generated_on: str = ""
    nodes_by_label: dict[str, int] = field(default_factory=dict)
    relationships_by_type: dict[str, int] = field(default_factory=dict)
    dangling_relationships_dropped: int = 0
    nodes_csv_sha256: str = ""
    rels_csv_sha256: str = ""

    @property
    def total_nodes(self) -> int:
        return sum(self.nodes_by_label.values())

    @property
    def total_relationships(self) -> int:
        return sum(self.relationships_by_type.values())

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "archive_sha256": self.archive_sha256,
            "archive_generated_on": self.archive_generated_on,
            "total_nodes": self.total_nodes,
            "total_relationships": self.total_relationships,
            "nodes_by_label": self.nodes_by_label,
            "relationships_by_type": self.relationships_by_type,
            "dangling_relationships_dropped": self.dangling_relationships_dropped,
            "nodes_csv_sha256": self.nodes_csv_sha256,
            "rels_csv_sha256": self.rels_csv_sha256,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        yield from csv.DictReader(fh)


def build(raw_dir: Path, out_dir: Path) -> Manifest:
    """Write `nodes.csv` and `rels.csv` for the Appleby subgraph."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest()

    archive = raw_dir / "full-oldb.zip"
    if archive.exists():
        manifest.archive_sha256 = _sha256(archive)
    stamp = next(raw_dir.glob("GENERATED_ON_*.txt"), None)
    if stamp is not None:
        manifest.archive_generated_on = stamp.name.removeprefix("GENERATED_ON_").removesuffix(
            ".txt"
        )

    # ── pass 1: nodes ──
    # `keep` maps node_id -> label rather than being a bare set, because the
    # relationship file needs its endpoints' labels written into it. Two
    # reasons: Kuzu's schema is typed, so a relationship table must declare
    # which node tables it connects; and on the Cypher engines an unlabelled
    # `MATCH (a {node_id: ...})` cannot use a per-label index, which would make
    # the ingest measurement a test of full scans rather than of ingest.
    keep: dict[str, str] = {}
    nodes_path = out_dir / "nodes.csv"
    with nodes_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(("node_id", "label", *NODE_PROPERTIES))
        for filename, label in NODE_FILES.items():
            path = raw_dir / filename
            if not path.exists():
                raise FileNotFoundError(f"{path} missing; run `gbench data --fetch` first")
            count = 0
            for row in _rows(path):
                if (row.get("sourceID") or "").strip() != SOURCE_ID:
                    continue
                node_id = (row.get("node_id") or "").strip()
                if not node_id or node_id in keep:
                    continue
                keep[node_id] = label
                writer.writerow(
                    (node_id, label, *((row.get(p) or "").strip() for p in NODE_PROPERTIES))
                )
                count += 1
            manifest.nodes_by_label[label] = count

    # ── pass 2: relationships, both endpoints required ──
    rels_path = out_dir / "rels.csv"
    with rels_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(("start_id", "start_label", "end_id", "end_label", "rel_type"))
        for row in _rows(raw_dir / "relationships.csv"):
            start = (row.get("node_id_start") or "").strip()
            end = (row.get("node_id_end") or "").strip()
            if start in keep and end in keep:
                # `rel_type` is free text upstream; normalised to a Cypher-safe
                # token so the identical type name reaches every engine.
                rel_type = (row.get("rel_type") or "related_to").strip()
                rel_type = "".join(c if c.isalnum() else "_" for c in rel_type).upper().strip("_")
                writer.writerow(
                    (start, keep[start], end, keep[end], rel_type or "RELATED_TO")
                )
                manifest.relationships_by_type[rel_type] = (
                    manifest.relationships_by_type.get(rel_type, 0) + 1
                )
            elif start in keep or end in keep:
                # Exactly one endpoint inside the subgraph: a real edge in the
                # full database that this subset cannot represent. Counted so
                # the README can state how much of the neighbourhood was cut.
                manifest.dangling_relationships_dropped += 1

    manifest.nodes_csv_sha256 = _sha256(nodes_path)
    manifest.rels_csv_sha256 = _sha256(rels_path)
    (out_dir / "manifest.json").write_text(json.dumps(manifest.as_dict(), indent=2) + "\n")
    return manifest


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    manifest = build(root / "data" / "raw", root / "data" / "build")
    print(f"source          {manifest.source_id}")
    print(f"archive         generated {manifest.archive_generated_on}")
    print(f"nodes           {manifest.total_nodes:,}")
    for label, n in sorted(manifest.nodes_by_label.items(), key=lambda kv: -kv[1]):
        print(f"  {label:<14}{n:>10,}")
    print(f"relationships   {manifest.total_relationships:,}")
    for rel, n in sorted(manifest.relationships_by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {rel:<14}{n:>10,}")
    dropped = manifest.dangling_relationships_dropped
    print(f"dropped         {dropped:,} dangling (one endpoint only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
