SHELL := /bin/sh
UV ?= uv
# 0016 不允许把缺少 immutable snapshot 的旧 active eval tree 猜值升级；
# revision-scoped 目录保留旧状态供人工 drain，同时保证验收可重复运行。
EVAL_STATE_DIR ?= $(CURDIR)/templates/service-app/.agent-harness/eval/0016

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
	$(UV) run python templates/service-app/app/migrate.py \
		--profile local \
		--profiles-dir "$(CURDIR)/templates/service-app/configs/profiles" \
		--storage-dsn "sqlite+aiosqlite:///$(EVAL_STATE_DIR)/eval.db"
	BUDGET_LEDGER_FINGERPRINT_KEY="local-eval-ephemeral-key" \
	$(UV) run python templates/service-app/scripts/run_example_evals.py \
		--state-dir "$(EVAL_STATE_DIR)"

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
