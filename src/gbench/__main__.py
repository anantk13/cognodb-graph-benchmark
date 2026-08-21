"""Command line entry point.

    gbench bench --arm capped|managed|all
    gbench report
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gbench import config as cfg
from gbench.orchestrator import new_run_dir, run_capped_arm, run_managed_arm

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gbench")
    sub = parser.add_subparsers(dest="command", required=True)

    bench = sub.add_parser("bench", help="run an arm and write raw results")
    bench.add_argument("--arm", choices=("capped", "managed", "all"), default="all")
    bench.add_argument("--quick", action="store_true", help="reduced iterations; not publishable")
    bench.add_argument("--run-dir", type=Path, default=None)
    bench.add_argument(
        "--only",
        default=None,
        help="comma-separated target ids; re-measure a subset into an existing run dir",
    )

    sub.add_parser("report", help="regenerate tables and charts from raw results")

    site = sub.add_parser("site", help="build the static results site from raw results")
    site.add_argument(
        "--repo",
        default="https://github.com/anantk13/cognodb-graph-benchmark",
        help="repository URL linked from the page footer",
    )

    args = parser.parse_args(argv)
    config = cfg.load()

    if args.command == "report":
        from gbench.report.generate import generate

        generate(ROOT / "results" / "raw", ROOT / "results")
        return 0

    if args.command == "site":
        from gbench.report.site import build

        path = build(
            ROOT / "results" / "raw",
            ROOT / "results" / "charts",
            ROOT / "site",
            repo_url=args.repo,
        )
        print(f"wrote {path}")
        return 0

    for skipped in config.skipped:
        print(f"! {skipped.explain()}")

    # A target whose licence has not been confirmed to permit publishing
    # benchmark results is flagged before the run, not after it. One widely
    # criticised vendor benchmark shipped a harness under terms that barred the
    # competitors it named from re-running it.
    unverified = config.unverified_licences()
    if unverified:
        print("! licence not yet confirmed to permit publication:")
        for target in unverified:
            print(f"!   {target.id}: {target.licence}")

    out_dir = args.run_dir or new_run_dir(ROOT / "results" / "raw")
    build_dir = ROOT / "data" / "build"
    print(f"writing to {out_dir}")

    only = {t.strip() for t in args.only.split(",")} if args.only else None

    if args.arm in ("managed", "all"):
        print("\n== arm B: managed free tiers ==")
        run_managed_arm(config, out_dir, build_dir=build_dir, quick=args.quick, only=only)

    if args.arm in ("capped", "all"):
        print("\n== arm A: identical cgroup limits ==")
        run_capped_arm(config, out_dir, build_dir=build_dir, quick=args.quick, only=only)

    print(f"\ndone. raw results in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
