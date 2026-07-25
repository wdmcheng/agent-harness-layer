SHELL := /bin/sh
UV ?= uv
PYTHON ?= python3
# 公开入口要求在项目根执行 make（直接进入目录或使用 make -C）；CURDIR
# 不会把路径中的空格误当成 Makefile 列表分隔符。
APP_ROOT := $(CURDIR)
PROFILE ?= local
PROFILES_DIR ?= $(APP_ROOT)/configs/profiles
HOST ?= 127.0.0.1
PORT ?= 8000
STATE_DIR ?= $(APP_ROOT)/.agent-harness/local
STORAGE_DSN ?= sqlite+aiosqlite:///$(STATE_DIR)/agent_harness.db
EVENTS_PATH ?= $(STATE_DIR)/traces.jsonl
AGENT_HARNESS_SOURCE ?=
AGENT_HARNESS_ALLOW_INDEX ?= 0

.PHONY: bootstrap dev cli run-basic run-rag run-ticket run-repo run-dev test contract quality pyright-environment migrate-service smoke-local smoke-service eval eval-rag eval-ticket eval-repo eval-dev worker

# 仓库内由根 workspace 提供核心包；复制出的模板必须显式提供本地 wheel/source，
# 或明确允许从已配置的可信 index 解析，避免误装同名公共包。
bootstrap:
	AGENT_HARNESS_SOURCE="$(AGENT_HARNESS_SOURCE)" \
	AGENT_HARNESS_ALLOW_INDEX="$(AGENT_HARNESS_ALLOW_INDEX)" \
	$(PYTHON) "$(APP_ROOT)/scripts/bootstrap.py" --uv "$(UV)"

# dev 和 run-basic 都允许调用方覆盖 STATE_DIR/DSN，自动化验证必须指向独立临时目录。
dev: bootstrap
	mkdir -p "$(STATE_DIR)"
	cd "$(APP_ROOT)" && $(UV) run agent-harness-service serve \
		--profile "$(PROFILE)" \
		--profiles-dir "$(PROFILES_DIR)" \
		--storage-dsn "$(STORAGE_DSN)" \
		--events-path "$(EVENTS_PATH)" \
		--host "$(HOST)" \
		--port "$(PORT)"

# 核心 CLI 的业务逻辑只保留一份；ARGS 由调用者传入，例如 `make cli ARGS='doctor'`。
cli: bootstrap
	cd "$(APP_ROOT)" && $(UV) run agent-harness $(ARGS)

run-basic: bootstrap
	mkdir -p "$(STATE_DIR)"
	cd "$(APP_ROOT)" && $(UV) run agent-harness run examples.basic \
		--profile "$(PROFILE)" \
		--profiles-dir "$(PROFILES_DIR)" \
		--agents-dir "$(APP_ROOT)/agents" \
		--storage-dsn "$(STORAGE_DSN)" \
		--events-path "$(EVENTS_PATH)"

# 四个示例都经核心 run composition；target 只提供可复制的安全输入。
run-rag: bootstrap
	cd "$(APP_ROOT)" && $(UV) run agent-harness run examples.rag_assistant \
		--profile "$(PROFILE)" --profiles-dir "$(PROFILES_DIR)" \
		--agents-dir "$(APP_ROOT)/agents" --storage-dsn "$(STORAGE_DSN)" \
		--events-path "$(EVENTS_PATH)" --prompt "unindexed roadmap item"

run-ticket: bootstrap
	cd "$(APP_ROOT)" && $(UV) run agent-harness run examples.ticket_triage \
		--profile "$(PROFILE)" --profiles-dir "$(PROFILES_DIR)" \
		--agents-dir "$(APP_ROOT)/agents" --storage-dsn "$(STORAGE_DSN)" \
		--events-path "$(EVENTS_PATH)" --prompt "production outage"

run-repo: bootstrap
	cd "$(APP_ROOT)" && $(UV) run agent-harness run examples.repo_analyst \
		--profile "$(PROFILE)" --profiles-dir "$(PROFILES_DIR)" \
		--agents-dir "$(APP_ROOT)/agents" --storage-dsn "$(STORAGE_DSN)" \
		--events-path "$(EVENTS_PATH)" --prompt "read README.md"

run-dev: bootstrap
	cd "$(APP_ROOT)" && $(UV) run agent-harness run examples.dev_assistant \
		--profile "$(PROFILE)" --profiles-dir "$(PROFILES_DIR)" \
		--agents-dir "$(APP_ROOT)/agents" --storage-dsn "$(STORAGE_DSN)" \
		--events-path "$(EVENTS_PATH)" --prompt "read README.md"

test: bootstrap
	cd "$(APP_ROOT)" && $(UV) run pytest tests -q

contract: test

pyright-environment: bootstrap
	cd "$(APP_ROOT)" && $(UV) run python scripts/check_pyright_environment.py

quality: pyright-environment
	cd "$(APP_ROOT)" && $(UV) run ruff check app agents tests scripts
	cd "$(APP_ROOT)" && $(UV) run pyright app agents tests scripts

# local smoke 只走复制产物内的 profile、registry 和公开 CLI，不依赖仓库根脚本。
smoke-local: bootstrap
	cd "$(APP_ROOT)" && $(UV) run agent-harness doctor \
		--profile local --profiles-dir "$(PROFILES_DIR)"
	cd "$(APP_ROOT)" && $(UV) run agent-harness agents list \
		--agents-dir "$(APP_ROOT)/agents"

# 只跑 migration 时仍需要 service profile 的 PostgreSQL/Redis 依赖已健康。
migrate-service: bootstrap
	cd "$(APP_ROOT)" && $(UV) run python scripts/smoke_service.py --migrate-only

smoke-service:
	@cd "$(APP_ROOT)" && $(PYTHON) scripts/smoke_service.py

# 评测库与开发库隔离；每个 target 必须在进入 fail-closed runtime 前
# 显式迁移它自己的 SQLite，不把自动迁移隐藏进 application factory。
define RUN_EXAMPLE_EVAL
	mkdir -p "$(STATE_DIR)/$(1)"
	cd "$(APP_ROOT)" && $(UV) run python app/migrate.py \
		--profile "$(PROFILE)" \
		--profiles-dir "$(PROFILES_DIR)" \
		--storage-dsn "sqlite+aiosqlite:///$(STATE_DIR)/$(1)/eval.db"
	cd "$(APP_ROOT)" && $(UV) run python scripts/run_example_evals.py \
		$(2) --state-dir "$(STATE_DIR)/$(1)"
endef

eval: bootstrap
	$(call RUN_EXAMPLE_EVAL,eval,)

eval-rag: bootstrap
	$(call RUN_EXAMPLE_EVAL,eval-rag,--agent examples.rag_assistant)

eval-ticket: bootstrap
	$(call RUN_EXAMPLE_EVAL,eval-ticket,--agent examples.ticket_triage)

eval-repo: bootstrap
	$(call RUN_EXAMPLE_EVAL,eval-repo,--agent examples.repo_analyst)

eval-dev: bootstrap
	$(call RUN_EXAMPLE_EVAL,eval-dev,--agent examples.dev_assistant)

# worker 入口和 API 使用同一套 RuntimeComponents；不要在模板里另起一套存储装配。
worker: bootstrap
	cd "$(APP_ROOT)" && $(UV) run python -m app.workers.runtime_worker
