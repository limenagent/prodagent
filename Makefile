.PHONY: lint test test-cov clean clean-data playground playground-prod config services-up services-down services-logs docs docs-refs docs-verify

# Auto-install uv if missing. uv manages Python too, so no separate Python install.
# Sourced as a target so every entry point gets the guarantee for free.
_ensure_uv:
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "uv not found, installing..."; \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
		export PATH="$$HOME/.local/bin:$$PATH"; \
	fi

playground: _ensure_uv
	@export PATH="$$HOME/.local/bin:$$PATH"; \
	uv sync --extra playground --extra openai --extra anthropic; \
	if [ ! -f .env ]; then \
		echo "First run — configuring LLM vendor (writing .env)"; \
		$(MAKE) config; \
	fi; \
	echo "Starting prodagent playground — http://127.0.0.1:8766"; \
	uv run prodagent --port 8766

# Prod backends: spin up Postgres / Neo4j / Redis via docker compose,
# then start the playground with PRODAGENT_BACKEND=prod. Default (make playground)
# stays on file + memory — zero dependency.
playground-prod: services-up _ensure_uv
	@export PATH="$$HOME/.local/bin:$$PATH"; \
	if [ ! -f .env ]; then \
		echo "First run — configuring LLM vendor (writing .env)"; \
		$(MAKE) config; \
	fi; \
	uv sync --extra playground --extra openai --extra anthropic --extra postgres --extra redis --extra neo4j --all-packages; \
	echo "Starting prodagent playground (prod backends) — http://127.0.0.1:8766"; \
	echo "  checkpoint/event/memory/span -> Postgres"; \
	echo "  entity/fact graph            -> Neo4j"; \
	echo "  cache/lock/idem/DLQ          -> Redis"; \
	PRODAGENT_BACKEND=prod \
	DATABASE_URL="postgres://postgres:prodagent@localhost:5433/prodagent" \
	REDIS_URL="redis://localhost:6390/0" \
	NEO4J_URI="bolt://localhost:7687" \
	NEO4J_USER="neo4j" \
	NEO4J_PASSWORD="password" \
	uv run prodagent --port 8766

# Start the three backing services. Idempotent — docker compose up -d skips
# containers that are already running. Requires docker.
services-up:
	@command -v docker >/dev/null 2>&1 || { echo "docker not found — install Docker Desktop first"; exit 1; }
	@docker compose up -d
	@echo "Waiting for services to be ready..."
	@for svc in postgres redis neo4j; do \
		i=0; \
		until docker inspect --format='{{.State.Health.Status}}' prodagent-$$svc 2>/dev/null | grep -q healthy; do \
			i=$$((i+1)); \
			if [ $$i -gt 30 ]; then echo "  $$svc not ready, timeout — check 'make services-logs'"; exit 1; fi; \
			printf "  %s..." $$svc; sleep 2; \
		done; \
		echo " ✓"; \
	done

services-down:
	@docker compose down

services-logs:
	@docker compose logs -f

config: _ensure_uv
	@export PATH="$$HOME/.local/bin:$$PATH"; \
	uv run python -m prodagent.playground.config_init

docs: _ensure_uv
	@export PATH="$$HOME/.local/bin:$$PATH"; \
	uv sync --extra dev --extra docs; \
	echo "Docs dev server — http://127.0.0.1:8000"; \
	uv run mkdocs serve

docs-refs: _ensure_uv
	@export PATH="$$HOME/.local/bin:$$PATH"; \
	uv run python scripts/check_docs_refs.py

# Mirrors the CI "docs" workflow's build job exactly — run before pushing
# doc changes so link/nav/reference breaks surface locally, not in CI.
docs-verify: _ensure_uv
	@export PATH="$$HOME/.local/bin:$$PATH"; \
	uv sync --extra dev --extra docs; \
	uv run python scripts/check_docs_refs.py; \
	uv run mkdocs build --strict

lint: _ensure_uv
	@export PATH="$$HOME/.local/bin:$$PATH"; \
	uv sync --extra dev; \
	uv run ruff check src/prodagent tests; \
	uv run ruff format --check src/prodagent tests; \
	uv run mypy src/prodagent

# Determinism boundary (Replay law), report-only until the soak ends —
# in-domain time/random/ids must come from prodagent.base.determinism.
lint-determinism: _ensure_uv
	@export PATH="$$HOME/.local/bin:$$PATH"; \
	uv run ruff check --select TID251 src/prodagent tests

format: _ensure_uv
	@export PATH="$$HOME/.local/bin:$$PATH"; \
	uv sync --extra dev; \
	uv run ruff format src/prodagent tests; \
	uv run ruff check --fix src/prodagent tests

test: _ensure_uv
	@export PATH="$$HOME/.local/bin:$$PATH"; \
	uv sync --extra dev; \
	uv run pytest tests/ -x -q

test-cov: _ensure_uv
	@export PATH="$$HOME/.local/bin:$$PATH"; \
	uv sync --extra dev; \
	uv run pytest tests/ --cov=prodagent --cov-report=term-missing --cov-report=html

clean:
	rm -rf .coverage htmlcov .pytest_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} +

	@for d in .prodagent examples/*/.prodagent src/prodagent/playground/.prodagent; do \
		if [ -d "$$d" ]; then echo "rm -rf $$d"; rm -rf "$$d"; fi; \
	done
	@if docker ps --format '{{.Names}}' | grep -q '^prodagent-postgres$$'; then \
		echo "TRUNCATE postgres tables"; \
		docker exec prodagent-postgres psql -U postgres -d prodagent \
			-c "TRUNCATE pa_memory, pa_session, pa_checkpoint, pa_event, pa_span;"; \
	else echo "skip postgres (container not running)"; fi
	@if docker ps --format '{{.Names}}' | grep -q '^prodagent-neo4j$$'; then \
		echo "DELETE neo4j graph"; \
		docker exec prodagent-neo4j cypher-shell -u neo4j -p password \
			"MATCH (n) DETACH DELETE n;"; \
	else echo "skip neo4j (container not running)"; fi
