#!/usr/bin/env bash
# Can each engine boot and answer a query inside each cgroup limit?
#
# Run before any measurement. A target that cannot start at a tier is a result
# -- reported as DNF with its exit code and OOM status -- not an error to work
# around by quietly raising the limit for that one engine.
#
# The check is deliberately "answered a query", not "the log said it started".
# Neo4j at 512m logs `Started.` and is then killed by the kernel a few seconds
# later; a log-based check reports that as a healthy target.
#
# Usage:  ./scripts/probe_tiers.sh [tier ...]     (default: 512m 1g 2g)

set -uo pipefail
export PATH="/opt/homebrew/bin:$PATH"

TIERS=("${@:-512m 1g 2g}")
read -r -a TIERS <<<"${TIERS[*]}"
CPUS=0.5
OUT="results/tier-probe.tsv"
mkdir -p results
printf 'engine\ttier\tstatus\tseconds\texit_code\toom_killed\tenforced_memory_max\n' >"$OUT"

# Engine internal memory budget, held at ~55% of the container cap on every
# engine. Left at their defaults these are wildly unequal -- Neo4j takes a fixed
# 512M heap plus 512M page cache regardless of the cgroup, Memgraph takes
# 90-100% of detected RAM, Kuzu takes 80% -- and that inequality would silently
# become the result.
budget_mb() {
  case "$1" in
  512m) echo 280 ;;
  1g) echo 560 ;;
  2g) echo 1120 ;;
  *) echo 280 ;;
  esac
}

cleanup() { docker rm -f probe >/dev/null 2>&1 || true; }
trap cleanup EXIT

record() { printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$@" >>"$OUT"; }

probe() {
  local engine=$1 tier=$2 budget elapsed=0 enforced="-"
  budget=$(budget_mb "$tier")
  cleanup

  case "$engine" in
  neo4j)
    docker run -d --name probe --cpus=$CPUS --memory="$tier" --memory-swap="$tier" \
      -e NEO4J_AUTH=neo4j/benchmarkpassword \
      -e NEO4J_server_memory_heap_initial__size="$((budget * 2 / 3))m" \
      -e NEO4J_server_memory_heap_max__size="$((budget * 2 / 3))m" \
      -e NEO4J_server_memory_pagecache_size="$((budget / 3))m" \
      neo4j:5.26-community >/dev/null 2>&1
    check() { docker exec probe cypher-shell -u neo4j -p benchmarkpassword "RETURN 1" >/dev/null 2>&1; }
    ;;
  memgraph)
    docker run -d --name probe --cpus=$CPUS --memory="$tier" --memory-swap="$tier" \
      memgraph/memgraph-mage:latest --memory-limit="$budget" --telemetry-enabled=false >/dev/null 2>&1
    check() { docker exec probe bash -lc 'echo "RETURN 1;" | mgconsole' >/dev/null 2>&1; }
    ;;
  falkordb)
    docker run -d --name probe --cpus=$CPUS --memory="$tier" --memory-swap="$tier" \
      -e REDIS_ARGS="--maxmemory ${budget}mb" \
      falkordb/falkordb:latest >/dev/null 2>&1
    check() { docker exec probe redis-cli GRAPH.QUERY probe "RETURN 1" >/dev/null 2>&1; }
    ;;
  *)
    echo "unknown engine: $engine" >&2
    return 1
    ;;
  esac

  for _ in $(seq 1 24); do
    if check; then
      enforced=$(docker exec probe cat /sys/fs/cgroup/memory.max 2>/dev/null || echo "-")
      record "$engine" "$tier" OK "$elapsed" - false "$enforced"
      printf '  %-10s %-5s OK in %ss (cgroup memory.max=%s)\n' "$engine" "$tier" "$elapsed" "$enforced"
      cleanup
      return 0
    fi
    if [ "$(docker inspect -f '{{.State.Status}}' probe 2>/dev/null)" != "running" ]; then
      local code oom
      code=$(docker inspect -f '{{.State.ExitCode}}' probe 2>/dev/null)
      oom=$(docker inspect -f '{{.State.OOMKilled}}' probe 2>/dev/null)
      record "$engine" "$tier" DNF "$elapsed" "$code" "$oom" -
      printf '  %-10s %-5s DNF after %ss (exit=%s oom_killed=%s)\n' "$engine" "$tier" "$elapsed" "$code" "$oom"
      cleanup
      return 0
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done

  record "$engine" "$tier" TIMEOUT "$elapsed" - - -
  printf '  %-10s %-5s TIMEOUT after %ss\n' "$engine" "$tier" "$elapsed"
  cleanup
}

for tier in "${TIERS[@]}"; do
  echo "── tier $tier (cpus=$CPUS, engine budget $(budget_mb "$tier")MB) ──"
  for engine in neo4j memgraph falkordb; do
    probe "$engine" "$tier"
  done
done

echo
echo "written to $OUT"
