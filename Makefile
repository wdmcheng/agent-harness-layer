SHELL := /bin/sh
UV ?= uv

.PHONY: sync quality test smoke-local smoke-service build license-check format

sync:
	$(UV) sync

quality:
	$(UV) run ruff format --check .
	$(UV) run ruff check .
	$(UV) run pyright
	$(UV) run python scripts/import_boundary_check.py

test:
	$(UV) run pytest

smoke-local:
	$(UV) run python scripts/smoke_local.py

smoke-service:
	$(UV) run python scripts/smoke_service.py

build:
	$(UV) build --package agent-harness --clear

license-check:
	$(UV) run python scripts/license_check.py

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .
