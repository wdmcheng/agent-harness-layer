SHELL := /bin/sh
UV ?= uv

.PHONY: migrate-service smoke-local smoke-service worker

# 模板 Makefile 始终回到 workspace 根执行脚本，避免复制出的 app 和当前 checkout
# 使用不同的依赖解析路径。
smoke-local:
	cd ../.. && uv run python scripts/smoke_local.py

# 只跑 migration 时仍需要 service profile 的 PostgreSQL/Redis 依赖已健康。
migrate-service:
	cd ../.. && $(UV) run python scripts/smoke_service.py --migrate-only

smoke-service:
	cd ../.. && $(UV) run python scripts/smoke_service.py

# worker 入口和 API 使用同一套 RuntimeComponents；不要在模板里另起一套存储装配。
worker:
	cd ../.. && $(UV) run python -m app.workers.runtime_worker
