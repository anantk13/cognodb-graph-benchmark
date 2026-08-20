
# One-command reproduction. Anyone with free-tier accounts should be able to
# get from a clone to a results table using only the targets below.

SHELL := /bin/bash
PY    := .venv/bin/python
UV    := uv

.DEFAULT_GOAL := help
.PHONY: help setup data probe smoke bench bench-capped bench-managed report lint test clean clean-docker

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Create the venv and install pinned dependencies
	$(UV) sync
	@test -f .env || (cp .env.example .env && \
	  echo "created .env -- fill in the connection details before running the managed arm")

data: ## Download the ICIJ archive and build the Appleby subgraph
	@mkdir -p data/raw
	@test -f data/raw/full-oldb.zip || \
	  curl -# -L -o data/raw/full-oldb.zip \
	    "https://offshoreleaks-data.icij.org/offshoreleaks/csv/full-oldb.LATEST.zip"
	@cd data/raw && unzip -oq full-oldb.zip
	$(PY) -m gbench.dataset.prepare

probe: ## Check which engines boot inside each memory cap (writes results/tier-probe.tsv)
	./scripts/probe_tiers.sh

smoke: ## End-to-end correctness check against one local engine
	$(PY) scripts/smoke.py 2g

bench: bench-capped bench-managed ## Run both arms

bench-capped: ## Arm A -- every engine under identical cgroup limits
	$(PY) -m gbench bench --arm capped

bench-managed: ## Arm B -- the managed free tiers as shipped
	$(PY) -m gbench bench --arm managed

report: ## Regenerate the README results tables and charts from raw results
	$(PY) -m gbench report

lint: ## Lint and format-check
	$(UV) run ruff check src/ scripts/
	$(UV) run ruff format --check src/

test: ## Run the unit tests
	$(UV) run pytest -q

clean: ## Remove build artefacts, keeping the downloaded archive
	rm -rf data/build results/raw .pytest_cache .ruff_cache

clean-docker: ## Remove containers this harness started
	-docker rm -f $$(docker ps -aq --filter "name=gbench-") 2>/dev/null
