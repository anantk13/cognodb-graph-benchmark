"""Container lifecycle for the capped arm.

Docker is driven through the CLI rather than a client library so that the exact
command is a string this module can record verbatim into the results file.
A reader should not have to trust a description of how the container was
limited; they should be able to copy the command and run it.

Readiness is decided by whether the engine answers a query, never by its log.
Neo4j at tight memory limits prints `Started.` and is killed by the kernel a few
seconds later, so a log-based check reports a dead container as healthy -- which
is how a DNF becomes a silently missing row rather than a published result.

Every engine's internal memory budget is set to the same fraction of its cgroup
limit. Left at their defaults they are wildly unequal: Neo4j takes a fixed 512M
heap plus 512M page cache regardless of the cgroup, Memgraph takes 90-100% of
detected RAM, Kuzu takes 80%. Those defaults would have become the result.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

DOCKER = "/opt/homebrew/bin/docker"

#: Fraction of the container's memory limit handed to the engine itself, with
#: the remainder left for the process, its allocator and the OS. Identical for
#: every engine, which is the entire point.
ENGINE_BUDGET_FRACTION = 0.55


@dataclass
class ContainerRun:
    """A started container and everything worth recording about it."""

    target_id: str
    tier_id: str
    image: str
    command: list[str]
    """The exact `docker run` invocation, recorded so the cap is reproducible."""

    started: bool = False
    ready: bool = False
    seconds_to_ready: float = 0.0
    exit_code: int | None = None
    oom_killed: bool = False
    enforced: dict[str, str] = field(default_factory=dict)
    """cgroup values read from inside the running container. The requested
    limit proves nothing; the enforced one proves the cap applied."""

    @property
    def dnf(self) -> bool:
        """Did not finish: started but never served a query."""
        return self.started and not self.ready

    def as_dict(self) -> dict:
        return {
            "target_id": self.target_id,
            "tier_id": self.tier_id,
            "image": self.image,
            "docker_command": " ".join(self.command),
            "ready": self.ready,
            "dnf": self.dnf,
            "seconds_to_ready": self.seconds_to_ready,
            "exit_code": self.exit_code,
            "oom_killed": self.oom_killed,
            "enforced_cgroup": self.enforced,
        }


def _docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [DOCKER, *args], capture_output=True, text=True, check=False, timeout=timeout
    )


def _budget_mb(memory: str) -> int:
    """Engine memory budget in MiB for a cgroup limit like '512m' or '2g'."""
    value = memory.strip().lower()
    mib = int(value[:-1]) * (1024 if value.endswith("g") else 1)
    return int(mib * ENGINE_BUDGET_FRACTION)


def container_name(target_id: str, tier_id: str) -> str:
    return f"gbench-{target_id}-{tier_id}"


def build_command(target_id: str, image: str, tier_id: str, memory: str, cpus: float,
                  port: int, host_port: int, password: str) -> list[str]:
    """Compose the `docker run` for one engine at one tier.

    `--memory-swap` equals `--memory` deliberately. Left unset, Docker grants
    swap equal to the memory limit again, so a container asked for 512m quietly
    receives 1g of addressable memory and the entire sweep measures the wrong
    thing.
    """
    budget = _budget_mb(memory)
    base = [
        "run", "-d", "--name", container_name(target_id, tier_id),
        f"--cpus={cpus}", f"--memory={memory}", f"--memory-swap={memory}",
        "-p", f"{host_port}:{port}",
    ]  # fmt: skip

    if target_id.startswith("neo4j"):
        return [
            *base,
            "-e", f"NEO4J_AUTH=neo4j/{password}",
            "-e", f"NEO4J_server_memory_heap_initial__size={budget*2//3}m",
            "-e", f"NEO4J_server_memory_heap_max__size={budget*2//3}m",
            "-e", f"NEO4J_server_memory_pagecache_size={budget//3}m",
            image,
        ]  # fmt: skip
    if target_id.startswith("memgraph"):
        return [*base, image, f"--memory-limit={budget}", "--telemetry-enabled=false"]
    if target_id.startswith("falkordb"):
        return [*base, "-e", f"REDIS_ARGS=--maxmemory {budget}mb", image]
    raise ValueError(f"no container recipe for target {target_id!r}")


def start(command: list[str], target_id: str, tier_id: str, image: str) -> ContainerRun:
    run = ContainerRun(target_id=target_id, tier_id=tier_id, image=image, command=command)
    _docker("rm", "-f", container_name(target_id, tier_id))
    result = _docker(*command)
    run.started = result.returncode == 0
    return run


def wait_ready(run: ContainerRun, probe: list[str], timeout_s: int = 180) -> ContainerRun:
    """Poll until the engine answers `probe`, it dies, or the timeout expires."""
    name = container_name(run.target_id, run.tier_id)
    started_at = time.perf_counter()
    deadline = started_at + timeout_s

    while time.perf_counter() < deadline:
        if _docker("exec", name, *probe, timeout=30).returncode == 0:
            run.ready = True
            run.seconds_to_ready = time.perf_counter() - started_at
            run.enforced = _read_cgroup(name)
            return run

        state = _docker("inspect", "-f", "{{.State.Status}}", name).stdout.strip()
        if state != "running":
            run.exit_code = _int_or_none(
                _docker("inspect", "-f", "{{.State.ExitCode}}", name).stdout.strip()
            )
            run.oom_killed = (
                _docker("inspect", "-f", "{{.State.OOMKilled}}", name).stdout.strip() == "true"
            )
            run.seconds_to_ready = time.perf_counter() - started_at
            return run
        time.sleep(5)

    run.seconds_to_ready = time.perf_counter() - started_at
    return run


def _read_cgroup(name: str) -> dict[str, str]:
    values = {}
    for key in ("cpu.max", "memory.max"):
        result = _docker("exec", name, "cat", f"/sys/fs/cgroup/{key}")
        values[key] = result.stdout.strip() if result.returncode == 0 else "unavailable"
    return values


def _int_or_none(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def stop(target_id: str, tier_id: str) -> None:
    _docker("rm", "-f", container_name(target_id, tier_id))


def probe_command(target_id: str, password: str) -> list[str]:
    """A shell command, run inside the container, that succeeds once serving."""
    if target_id.startswith("neo4j"):
        return ["cypher-shell", "-u", "neo4j", "-p", password, "RETURN 1"]
    if target_id.startswith("memgraph"):
        return ["bash", "-lc", 'echo "RETURN 1;" | mgconsole']
    if target_id.startswith("falkordb"):
        return ["redis-cli", "GRAPH.QUERY", "readiness", "RETURN 1"]
    raise ValueError(f"no readiness probe for target {target_id!r}")
