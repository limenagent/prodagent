.PHONY: install install-dev run run-fake test test-cov evals evals-baseline evals-ci lint format type-check clean

install:
	uv sync

install-dev:
	uv sync --extra dev

run:
	uv run python -m aiops.agent

run-fake:
	USE_FAKE_LLM=true uv run python -m aiops.agent

test:
	uv run python -m evals.runner --smoke

test-cov:
	uv run python -m evals.runner --smoke

evals:
	uv run python -m evals.runner --smoke

evals-baseline:
	uv run python -m evals.runner --baseline

evals-ci:
	uv run python -m evals.runner --ci

lint:
	ruff check aiops evals
	ruff format --check aiops evals

format:
	ruff format aiops evals
	ruff check --fix aiops evals

clean:
	rm -rf build dist *.egg-info .coverage htmlcov .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
