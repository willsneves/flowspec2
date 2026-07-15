UV_RUN := uv run --locked --extra dev --no-env-file
RELEASE_ARTIFACTS ?= build/release

.PHONY: ci format lint package-check release-build release-check test typecheck

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

release-build:
	$(UV_RUN) python scripts/check_package.py --artifact-directory "$(RELEASE_ARTIFACTS)" --build

release-check:
	$(UV_RUN) python scripts/check_package.py --artifact-directory "$(RELEASE_ARTIFACTS)"

format:
	$(UV_RUN) ruff check --fix .
	$(UV_RUN) ruff format .
