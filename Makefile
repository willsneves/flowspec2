UV_RUN := uv run --locked --extra dev --no-env-file

.PHONY: ci format lint test typecheck

lint:
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .

typecheck:
	$(UV_RUN) mypy
	$(UV_RUN) python scripts/run_pyright.py

test:
	$(UV_RUN) pytest

ci: lint typecheck test

format:
	$(UV_RUN) ruff check --fix .
	$(UV_RUN) ruff format .
