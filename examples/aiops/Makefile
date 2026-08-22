.PHONY: install install-dev run run-fake lint format type-check clean

install:
	uv sync

install-dev:
	uv sync --extra dev

run:
	uv run python -m aiops.agent

run-fake:
	USE_FAKE_LLM=true uv run python -m aiops.agent

lint:
	ruff check aiops
	ruff format --check aiops

format:
	ruff format aiops
	ruff check --fix aiops

clean:
	rm -rf build dist *.egg-info .coverage htmlcov .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
