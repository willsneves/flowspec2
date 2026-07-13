UV_RUN := uv run --locked --extra dev --no-env-file

.PHONY: ci format lint package-check test typecheck

lint:
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .

typecheck:
	$(UV_RUN) mypy
	$(UV_RUN) python scripts/run_pyright.py

test:
	$(UV_RUN) pytest

ci: lint typecheck test

package-check:
	$(UV_RUN) python scripts/check_package.py

format:
	$(UV_RUN) ruff check --fix .
	$(UV_RUN) ruff format .
