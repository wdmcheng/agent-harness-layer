SHELL := /bin/sh
UV ?= uv

.PHONY: migrate-service smoke-local smoke-service worker

smoke-local:
	cd ../.. && uv run python scripts/smoke_local.py

migrate-service:
	cd ../.. && $(UV) run python scripts/smoke_service.py --migrate-only

smoke-service:
	cd ../.. && $(UV) run python scripts/smoke_service.py

worker:
	cd ../.. && $(UV) run python -m app.workers.runtime_worker
