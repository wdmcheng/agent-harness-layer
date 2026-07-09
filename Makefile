SHELL := /bin/sh
UV ?= uv

.PHONY: sync quality test smoke-local smoke-service eval build license-check format

# 根 Makefile 是 reviewer 和 CI 的统一入口；各 target 保持薄包装，实际边界说明
# 留在对应 Python 脚本和 pyproject 工具配置里。
sync:
	$(UV) sync

quality:
	$(UV) run ruff format --check .
	$(UV) run ruff check .
	$(UV) run pyright
	$(UV) run python scripts/import_boundary_check.py

test:
	$(UV) run pytest

eval:
	$(UV) run python -m agent_harness.cli eval run

# local smoke 只证明离线 profile 和 CLI/template shell 可用，不替代 service smoke。
smoke-local:
	$(UV) run python scripts/smoke_local.py

# service smoke 需要 Docker Compose、PostgreSQL 和 Redis；不能用 SQLite 结果替代。
smoke-service:
	$(UV) run python scripts/smoke_service.py

build:
	$(UV) build --package agent-harness --clear

license-check:
	$(UV) run python scripts/license_check.py

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .
