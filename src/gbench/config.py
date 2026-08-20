"""Load `config/targets.yaml` and resolve credentials from the environment.

Two rules this module exists to keep.

**No credential ever appears in a file that git tracks.** `targets.yaml` names
the environment variables a target needs; it never holds their values. The
values come from `.env`, which is gitignored, or from the real environment.
A target whose variables are unset is skipped with an explanation, not run
against a placeholder.

**A vendor's claim and this harness's measurement never share a field.** The
`advertised` block on every target is quoted from that vendor's own page with
the URL attached, and `None` means the vendor does not publish the figure. It
is rendered as "not published" and never filled in with an estimate. Measured
numbers live in `results/` and are joined to these claims only at report time,
where the join is visible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "targets.yaml"


class ConfigError(RuntimeError):
    """Raised when the configuration is internally inconsistent."""


@dataclass(frozen=True)
class MissingCredentials:
    """A target that cannot run because its environment variables are unset.

    Deliberately a value rather than an exception: the correct behaviour when
    one target's credentials are missing is to run the others and say plainly
    in the report which target did not run and why. Silently omitting it, or
    aborting the whole suite, are both worse.
    """

    target_id: str
    missing: list[str]

    def explain(self) -> str:
        return (
            f"{self.target_id}: skipped, unset environment variable(s): "
            f"{', '.join(self.missing)}. Copy .env.example to .env and fill them in."
        )


@dataclass(frozen=True)
class Target:
    """One database the harness can measure."""

    id: str
    arm: str
    adapter: str
    dialect: str
    credentials: dict[str, str]
    """Resolved values, keyed by the logical name used in targets.yaml
    (`uri`, `user`, `password`, ...). Never logged, never serialised."""

    advertised: dict[str, Any] = field(default_factory=dict)
    """The vendor's own published claims, verbatim, with a `source` URL."""

    image: str | None = None
    port: int | None = None
    licence: str | None = None
    licence_permits_benchmark_publication: bool | None = None
    """None means unverified. The report refuses to publish a result for a
    target whose licence has not been checked -- one widely criticised vendor
    benchmark shipped a harness under terms that barred the competitors it
    named from re-running it, and the point of this field is to make the
    equivalent mistake impossible here rather than merely unlikely."""

    report_separately: bool = False
    note: str | None = None
    load_paths_not_used: list[str] = field(default_factory=list)
    """Faster ingest mechanisms this engine offers that were deliberately not
    used, so the README can list them beside the uniform method that was."""

    def redacted(self) -> dict[str, Any]:
        """Everything about this target that is safe to write into results."""
        return {
            "id": self.id,
            "arm": self.arm,
            "adapter": self.adapter,
            "dialect": self.dialect,
            "image": self.image,
            "advertised": self.advertised,
            "licence": self.licence,
            "report_separately": self.report_separately,
            "note": self.note,
            "load_paths_not_used": self.load_paths_not_used,
        }


@dataclass(frozen=True)
class Tier:
    """One rung of the capped arm's memory sweep."""

    id: str
    cpus: float
    memory: str
    note: str = ""

    def docker_args(self) -> list[str]:
        """Container limits.

        `--memory-swap` is set equal to `--memory` on purpose. Left unset,
        Docker grants swap equal to the memory limit again, so a container asked
        for 512m quietly receives 1g of addressable memory and the whole sweep
        measures the wrong thing.

        The values these produce are read back from `/sys/fs/cgroup` inside the
        running container and recorded in the results, so the report can show
        the enforced limit rather than the requested one.
        """
        return [
            f"--cpus={self.cpus}",
            f"--memory={self.memory}",
            f"--memory-swap={self.memory}",
        ]


@dataclass(frozen=True)
class Config:
    dataset: dict[str, Any]
    run: dict[str, Any]
    indexes: list[tuple[str, str]]
    tiers: list[Tier]
    targets: list[Target]
    skipped: list[MissingCredentials]
    excluded: list[dict[str, str]]
    """Databases considered and rejected, with the reasoning. Rendered in the
    README: the assignment scores database selection, so an argued exclusion is
    part of the answer."""

    def by_arm(self, arm: str) -> list[Target]:
        return [t for t in self.targets if t.arm == arm]

    def get(self, target_id: str) -> Target:
        for t in self.targets:
            if t.id == target_id:
                return t
        raise KeyError(f"no target {target_id!r}")

    def unverified_licences(self) -> list[Target]:
        """Targets whose licence has not been confirmed to permit publication."""
        return [t for t in self.targets if t.licence_permits_benchmark_publication is not True]


def load(path: Path | None = None, *, env_file: Path | None = None) -> Config:
    """Read the configuration and resolve credentials from the environment."""
    config_path = path or DEFAULT_CONFIG
    if not config_path.exists():
        raise ConfigError(f"no config at {config_path}")

    load_dotenv(env_file or REPO_ROOT / ".env", override=False)
    raw = yaml.safe_load(config_path.read_text())

    tiers = [
        Tier(id=t["id"], cpus=float(t["cpus"]), memory=str(t["memory"]), note=t.get("note", ""))
        for t in raw.get("arms", {}).get("capped", {}).get("tiers", [])
    ]

    targets: list[Target] = []
    skipped: list[MissingCredentials] = []

    for arm_name, arm in raw.get("arms", {}).items():
        for spec in arm.get("targets", []):
            credentials, missing = _resolve_env(
                spec.get("env", {}), set(spec.get("blank_credentials_ok", []))
            )
            if missing:
                skipped.append(MissingCredentials(spec["id"], missing))
                continue
            targets.append(
                Target(
                    id=spec["id"],
                    arm=arm_name,
                    adapter=spec["adapter"],
                    dialect=spec["dialect"],
                    credentials=credentials,
                    advertised=spec.get("advertised") or {},
                    image=spec.get("image"),
                    port=spec.get("port"),
                    licence=spec.get("licence"),
                    licence_permits_benchmark_publication=spec.get(
                        "licence_permits_benchmark_publication"
                    ),
                    report_separately=bool(spec.get("report_separately", False)),
                    note=spec.get("note"),
                    load_paths_not_used=spec.get("load_paths_not_used") or [],
                )
            )

    indexes = [(pair[0], pair[1]) for pair in raw.get("indexes", [])]
    if not indexes:
        raise ConfigError(
            "no indexes declared; every target must be given the identical index set, "
            "and an empty set is far more likely to be an omission than a decision"
        )

    return Config(
        dataset=raw.get("dataset", {}),
        run=raw.get("run", {}),
        indexes=indexes,
        tiers=tiers,
        targets=targets,
        skipped=skipped,
        excluded=raw.get("excluded", []),
    )


def _is_placeholder(value: str) -> bool:
    """True if the value is still the template text from `.env.example`.

    `neo4j+s://<instance-id>.databases.neo4j.io` is not blank, so a blank check
    passes it, and the run then dies on an opaque DNS failure some minutes in.
    Copying the example file and filling in only some of it is the single most
    likely setup mistake, so it is worth catching by name.
    """
    return "<" in value and ">" in value


def _resolve_env(
    env_spec: dict[str, str], blank_ok: set[str]
) -> tuple[dict[str, str], list[str]]:
    """Map logical credential names to their values, reporting what is unset.

    A blank value counts as missing unless the target lists that credential in
    `blank_ok`. Memgraph genuinely runs unauthenticated, so its empty password
    is a real configuration; a blank CognoDB password is an unfilled `.env`.
    Treating the two the same lets the second one through to fail later as an
    opaque authentication error, several minutes into a run.
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for logical_name, var_name in env_spec.items():
        value = os.environ.get(var_name)
        if value is None:
            missing.append(var_name)
        elif _is_placeholder(value):
            missing.append(f"{var_name} (still the .env.example placeholder)")
        elif not value.strip() and logical_name not in blank_ok:
            missing.append(var_name)
        else:
            resolved[logical_name] = value
    return resolved, missing
