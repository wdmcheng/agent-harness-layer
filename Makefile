SHELL := /bin/sh
UV ?= uv
# 0016 不允许把缺少 immutable snapshot 的旧 active eval tree 猜值升级；
# revision-scoped 目录保留旧状态供人工 drain，同时保证验收可重复运行。
EVAL_STATE_DIR ?= $(CURDIR)/templates/service-app/.agent-harness/eval/0016
RELEASE_PREVIEW_DIR ?= $(CURDIR)/.artifacts/release-preview/current
RELEASE_MANIFEST ?= $(RELEASE_PREVIEW_DIR)/manifest.json
RELEASE_PROMOTION_PLAN ?= $(CURDIR)/.artifacts/release-promotion/plan.json
RELEASE_GITLAB_CHILD ?= $(CURDIR)/.artifacts/release-promotion/gitlab-child.yml
RELEASE_OUTPUT_DIR ?= $(CURDIR)/.artifacts/release-promotion/execute
RELEASE_PROMOTION_RECEIPT ?= $(RELEASE_OUTPUT_DIR)/receipt.json
RELEASE_BUILD_MANIFEST ?= $(CURDIR)/.artifacts/release-build/execute/manifest.json
REGISTRY_PLAN ?= $(CURDIR)/.artifacts/registry-publish/plan.json
REGISTRY_OUTPUT ?= $(CURDIR)/.artifacts/registry-publish/receipt.json

.PHONY: sync lock install quality ruff-format ruff-lint pyright pyright-environment import-boundary \
	unit-contract integration quality-aggregate test-aggregate test smoke-local \
	smoke-service smoke-live-model smoke-live-model-stream eval build license-check release-dry-run release-promote-plan \
	release-promote-execute registry-publish-plan registry-publish-execute format \
	ci-run ci-contract ci-contract-check acceptance-validate-check ci-acceptance-validate ci-history \
	ci-lock ci-install ci-ruff-format ci-ruff-lint ci-pyright ci-import-boundary \
	ci-unit-contract ci-integration ci-quality-aggregate ci-test-aggregate ci-eval \
	ci-smoke-local ci-smoke-service ci-smoke-live-model ci-smoke-live-model-stream ci-build ci-license ci-release-dry-run \
	ci-release-promote-plan ci-release-promote-execute ci-registry-publish-plan \
	ci-registry-publish-execute

# 根 Makefile 是 reviewer 和 CI 的统一入口；各 target 保持薄包装，实际边界说明
# 留在对应 Python 脚本和 pyproject 工具配置里。
sync:
	$(UV) sync

lock:
	$(UV) lock --check

install:
	$(UV) sync --frozen

ruff-format:
	$(UV) run ruff format --check .

ruff-lint:
	$(UV) run ruff check .

pyright-environment:
	$(UV) run python scripts/check_pyright_environment.py

pyright: pyright-environment
	$(UV) run pyright

import-boundary:
	$(UV) run python scripts/import_boundary_check.py

quality: pyright-environment
	$(UV) run ruff format --check .
	$(UV) run ruff check .
	$(UV) run pyright
	$(UV) run python scripts/import_boundary_check.py

test:
	$(UV) run --group release pytest

# CI 的 unit/contract seam 额外生成 JUnit 与 coverage；根 test 仍保留原有全量行为。
unit-contract:
	mkdir -p .artifacts/tests
	$(UV) run --group release coverage erase
	$(UV) run --group release coverage run \
		--source=packages/agent-harness/src/agent_harness,scripts,templates/service-app/app \
		-m pytest tests/contracts \
		--junitxml=.artifacts/tests/unit-contract-junit.xml
	$(UV) run --group release coverage xml -o .artifacts/tests/coverage.xml

integration:
	mkdir -p .artifacts/tests
	$(UV) run pytest tests/integration --junitxml=.artifacts/tests/integration-junit.xml

# 聚合命令是 AC-051 的独立证据，不由细粒度门禁代替。
quality-aggregate:
	$(MAKE) quality

test-aggregate:
	$(MAKE) test

eval:
	rm -f .artifacts/eval/scores.jsonl .artifacts/eval/traces.jsonl
	AGENT_HARNESS_BUDGET__FINGERPRINT_KEY="local-eval-ephemeral-key" \
	$(UV) run python templates/service-app/app/migrate.py \
		--profile local \
		--profiles-dir "$(CURDIR)/templates/service-app/configs/profiles" \
		--storage-dsn "sqlite+aiosqlite:///$(EVAL_STATE_DIR)/eval.db"
	AGENT_HARNESS_BUDGET__FINGERPRINT_KEY="local-eval-ephemeral-key" \
	$(UV) run python templates/service-app/scripts/run_example_evals.py \
		--state-dir "$(EVAL_STATE_DIR)"
	mkdir -p .artifacts/eval
	cp "$(EVAL_STATE_DIR)/scores.jsonl" .artifacts/eval/scores.jsonl
	cp "$(EVAL_STATE_DIR)/traces.jsonl" .artifacts/eval/traces.jsonl

# local smoke 只证明离线 profile 和 CLI/template shell 可用，不替代 service smoke。
smoke-local:
	$(UV) run python scripts/smoke_local.py

# service smoke 需要 Docker Compose、PostgreSQL 和 Redis；不能用 SQLite 结果替代。
smoke-service:
	$(UV) run python scripts/smoke_service.py

# 真实 smoke 与默认离线门禁隔离；无本会话授权时只写 hosted-unverified。
smoke-live-model:
	$(UV) run python scripts/smoke_live_model.py

# 增量 smoke 使用独立双授权和证据身份；默认路径零网络并报告 hosted-unverified。
smoke-live-model-stream:
	$(UV) run python -m scripts.smoke_live_model_stream

build:
	$(UV) build --package agent-harness --clear
	$(UV) run python scripts/build_checksum.py

license-check:
	$(UV) run --group license python scripts/license_check.py

# release change 拥有 CLI/schema；本入口只调用其默认 dry-run seam，不复制参数。
release-dry-run:
	$(UV) run --group release python scripts/release_dry_run.py --output-dir "$(RELEASE_PREVIEW_DIR)"

# plan 入口只冻结去敏身份；execute 入口消费同一 artifact 并动态注入其摘要。
# 凭据仍只允许由受保护 job 通过环境提供，Makefile 不保存或回显任何 token。
release-promote-plan:
	@test -f "$(RELEASE_MANIFEST)" || { echo "release-promote-plan: manifest is required: $(RELEASE_MANIFEST)" >&2; exit 2; }
	$(UV) run --group release python scripts/release_promote.py \
		--repo "$(CURDIR)" --manifest "$(RELEASE_MANIFEST)" \
		--plan-output "$(RELEASE_PROMOTION_PLAN)"
	$(UV) run --group release python scripts/release_gitlab_pipeline.py \
		--plan "$(RELEASE_PROMOTION_PLAN)" --output "$(RELEASE_GITLAB_CHILD)"
	@if [ -n "$${GITHUB_OUTPUT:-}" ]; then \
		status="$$(PLAN_PATH="$(RELEASE_PROMOTION_PLAN)" $(UV) run python -c 'import json, os; from pathlib import Path; print(json.loads(Path(os.environ["PLAN_PATH"]).read_text(encoding="utf-8"))["status"])')"; \
		if [ "$$status" = "planned" ]; then required=true; else required=false; fi; \
		printf '%s\n' "release_required=$$required" >> "$$GITHUB_OUTPUT"; \
	fi

release-promote-execute:
	@test -f "$(RELEASE_MANIFEST)" || { echo "release-promote-execute: manifest is required: $(RELEASE_MANIFEST)" >&2; exit 2; }
	@test -f "$(RELEASE_PROMOTION_PLAN)" || { echo "release-promote-execute: plan is required: $(RELEASE_PROMOTION_PLAN)" >&2; exit 2; }
	@set -eu; \
	plan_status="$$(PLAN_PATH="$(RELEASE_PROMOTION_PLAN)" $(UV) run python -c 'import json, os; from pathlib import Path; print(json.loads(Path(os.environ["PLAN_PATH"]).read_text(encoding="utf-8"))["status"])')"; \
	if [ "$$plan_status" = "no-release" ]; then \
		$(UV) run --group release python scripts/release_promote.py \
			--repo "$(CURDIR)" --manifest "$(RELEASE_MANIFEST)" \
			--output-dir "$(RELEASE_OUTPUT_DIR)" \
			--plan-input "$(RELEASE_PROMOTION_PLAN)" --execute; \
		exit 0; \
	fi; \
	: "$${RELEASE_PUSH_TOKEN:?restricted RELEASE_PUSH_TOKEN environment credential is required}"; \
	push_endpoint="$$(git remote get-url --push origin)"; \
	export PUSH_ENDPOINT="$$push_endpoint"; \
	export RELEASE_PUSH_HOST="$$( $(UV) run python -c 'import os; from urllib.parse import urlsplit; print(urlsplit(os.environ["PUSH_ENDPOINT"]).hostname or "")' )"; \
	export RELEASE_PUSH_PATH="$$( $(UV) run python -c 'import os; from urllib.parse import urlsplit; print(urlsplit(os.environ["PUSH_ENDPOINT"]).path.lstrip("/"))' )"; \
	test -n "$$RELEASE_PUSH_HOST"; test -n "$$RELEASE_PUSH_PATH"; \
	git config --local credential.useHttpPath true; \
	git config --local credential.helper '!f() { test "$$1" = get || exit 0; protocol=; host=; path=; while IFS="=" read -r key value; do case "$$key" in protocol) protocol="$$value" ;; host) host="$$value" ;; path) path="$$value" ;; esac; done; test "$$protocol" = https; test "$$host" = "$$RELEASE_PUSH_HOST"; test "$$path" = "$$RELEASE_PUSH_PATH"; printf "%s\n" "username=release-token" "password=$$RELEASE_PUSH_TOKEN"; }; f'; \
	trap 'git config --local --unset-all credential.helper >/dev/null 2>&1 || true; git config --local --unset-all credential.useHttpPath >/dev/null 2>&1 || true' EXIT HUP INT TERM; \
	export RELEASE_PROMOTION_APPROVAL_SHA256="$$(PLAN_PATH="$(RELEASE_PROMOTION_PLAN)" $(UV) run python -c 'import json, os; from pathlib import Path; print(json.loads(Path(os.environ["PLAN_PATH"]).read_text(encoding="utf-8"))["approval_sha256"])')"; \
	$(UV) run --group release python scripts/release_promote.py \
		--repo "$(CURDIR)" --manifest "$(RELEASE_MANIFEST)" \
		--output-dir "$(RELEASE_OUTPUT_DIR)" \
		--plan-input "$(RELEASE_PROMOTION_PLAN)" --execute

registry-publish-plan:
	@test -f "$(RELEASE_MANIFEST)" || { echo "registry-publish-plan: manifest is required: $(RELEASE_MANIFEST)" >&2; exit 2; }
	@test -f "$(RELEASE_PROMOTION_RECEIPT)" || { echo "registry-publish-plan: promoted receipt is required: $(RELEASE_PROMOTION_RECEIPT)" >&2; exit 2; }
	@test -f "$(RELEASE_BUILD_MANIFEST)" || { echo "registry-publish-plan: build manifest is required: $(RELEASE_BUILD_MANIFEST)" >&2; exit 2; }
	RELEASE_PROTECTED_REF_NAME="$$(RECEIPT_PATH="$(RELEASE_PROMOTION_RECEIPT)" $(UV) run python -c 'import json, os; from pathlib import Path; value=json.loads(Path(os.environ["RECEIPT_PATH"]).read_text(encoding="utf-8")); print("refs/tags/" + value["tag"])')" \
	RELEASE_PROTECTED_REF_SHA="$$(RECEIPT_PATH="$(RELEASE_PROMOTION_RECEIPT)" $(UV) run python -c 'import json, os; from pathlib import Path; print(json.loads(Path(os.environ["RECEIPT_PATH"]).read_text(encoding="utf-8"))["release_commit_sha"])')" \
	$(UV) run --group release python scripts/registry_publish.py \
		--manifest "$(RELEASE_MANIFEST)" \
		--promotion-receipt "$(RELEASE_PROMOTION_RECEIPT)" \
		--build-manifest "$(RELEASE_BUILD_MANIFEST)" \
		--artifact-root "$(CURDIR)" --output "$(REGISTRY_OUTPUT)" \
		--plan-output "$(REGISTRY_PLAN)"

registry-publish-execute:
	@test -f "$(REGISTRY_PLAN)" || { echo "registry-publish-execute: plan is required: $(REGISTRY_PLAN)" >&2; exit 2; }
	RELEASE_PROTECTED_REF_NAME="$$(RECEIPT_PATH="$(RELEASE_PROMOTION_RECEIPT)" $(UV) run python -c 'import json, os; from pathlib import Path; value=json.loads(Path(os.environ["RECEIPT_PATH"]).read_text(encoding="utf-8")); print("refs/tags/" + value["tag"])')" \
	RELEASE_PROTECTED_REF_SHA="$$(RECEIPT_PATH="$(RELEASE_PROMOTION_RECEIPT)" $(UV) run python -c 'import json, os; from pathlib import Path; print(json.loads(Path(os.environ["RECEIPT_PATH"]).read_text(encoding="utf-8"))["release_commit_sha"])')" \
	REGISTRY_PUBLISH_APPROVAL_SHA256="$$(PLAN_PATH="$(REGISTRY_PLAN)" $(UV) run python -c 'import json, os; from pathlib import Path; print(json.loads(Path(os.environ["PLAN_PATH"]).read_text(encoding="utf-8"))["approval_sha256"])')" \
	$(UV) run --group release python scripts/registry_publish.py \
		--manifest "$(RELEASE_MANIFEST)" \
		--promotion-receipt "$(RELEASE_PROMOTION_RECEIPT)" \
		--build-manifest "$(RELEASE_BUILD_MANIFEST)" \
		--artifact-root "$(CURDIR)" --output "$(REGISTRY_OUTPUT)" \
		--plan-input "$(REGISTRY_PLAN)" --execute

# 所有 CI gate 统一经 evidence runner 执行；GATE 只能由脚本 allowlist 映射到 Make target。
ci-run:
	@test -n "$(GATE)" || { echo "ci-run: GATE is required" >&2; exit 2; }
	$(UV) run python scripts/ci_evidence.py --gate "$(GATE)" $(CI_ARTIFACTS)

ci-contract:
	$(MAKE) ci-run GATE=ci-contract

ci-contract-check:
	$(UV) run python scripts/ci_contract.py

acceptance-validate-check:
	$(UV) run python scripts/acceptance_matrix.py

ci-acceptance-validate:
	$(MAKE) ci-run GATE=acceptance-validate

ci-history:
	$(UV) run python scripts/ci_history.py $(if $(EXPECTED_TAG),--expected-tag "$(EXPECTED_TAG)",)

ci-lock:
	$(MAKE) ci-run GATE=lock

ci-install:
	$(MAKE) ci-run GATE=install

ci-ruff-format:
	$(MAKE) ci-run GATE=ruff-format

ci-ruff-lint:
	$(MAKE) ci-run GATE=ruff-lint

ci-pyright:
	$(MAKE) ci-run GATE=pyright

ci-import-boundary:
	$(MAKE) ci-run GATE=import-boundary

ci-unit-contract:
	$(MAKE) ci-run GATE=unit-contract CI_ARTIFACTS="--artifact junit=.artifacts/tests/unit-contract-junit.xml --artifact coverage=.artifacts/tests/coverage.xml"

ci-integration:
	$(MAKE) ci-run GATE=integration CI_ARTIFACTS="--artifact junit=.artifacts/tests/integration-junit.xml"

ci-quality-aggregate:
	$(MAKE) ci-run GATE=quality-aggregate

ci-test-aggregate:
	$(MAKE) ci-run GATE=test-aggregate

ci-eval:
	$(MAKE) ci-run GATE=eval CI_ARTIFACTS="--artifact scores=.artifacts/eval/scores.jsonl --artifact traces=.artifacts/eval/traces.jsonl"

ci-smoke-local:
	$(MAKE) ci-run GATE=smoke-local CI_ARTIFACTS="--artifact trace=.artifacts/smoke/local/trace.jsonl"

ci-smoke-service:
	$(MAKE) ci-run GATE=smoke-service CI_ARTIFACTS="--artifact trace=.artifacts/smoke/service/trace.jsonl"

ci-smoke-live-model:
	$(MAKE) ci-run GATE=smoke-live-model CI_ARTIFACTS="--artifact live-result=.artifacts/smoke/live-model/result.json"

ci-smoke-live-model-stream:
	$(MAKE) ci-run GATE=smoke-live-model-stream CI_ARTIFACTS="--artifact live-stream-result=.artifacts/smoke/live-model-stream/result.json"

ci-build:
	$(MAKE) ci-run GATE=build CI_ARTIFACTS="--artifact wheel=dist/*.whl --artifact sdist=dist/*.tar.gz --artifact checksum=dist/SHA256SUMS"

ci-license:
	$(MAKE) ci-run GATE=license CI_ARTIFACTS="--artifact license-report=.artifacts/license/license-report.json --artifact smoke-evidence=.artifacts/license/smoke-service.log"

ci-release-dry-run: ci-history
	$(MAKE) ci-run GATE=release-dry-run CI_ARTIFACTS="--artifact release-preview=.artifacts/release-preview/current/manifest.json"

ci-release-promote-plan:
	$(MAKE) ci-run GATE=release-promote-plan CI_ARTIFACTS="--artifact promotion-plan=.artifacts/release-promotion/plan.json"

ci-release-promote-execute:
	@status="$$(PLAN_PATH="$(RELEASE_PROMOTION_PLAN)" $(UV) run python -c 'import json, os; from pathlib import Path; print(json.loads(Path(os.environ["PLAN_PATH"]).read_text(encoding="utf-8"))["status"])')"; \
	if [ "$$status" = "no-release" ]; then \
		$(MAKE) ci-run GATE=release-promote-execute CI_ARTIFACTS="--artifact promotion-receipt=.artifacts/release-promotion/execute/receipt.json"; \
	else \
		$(MAKE) ci-run GATE=release-promote-execute CI_ARTIFACTS="--artifact promotion-receipt=.artifacts/release-promotion/execute/receipt.json --artifact release-build=.artifacts/release-build/execute/manifest.json --artifact release-dist=.artifacts/release-build/execute/dist"; \
	fi

ci-registry-publish-plan:
	$(MAKE) ci-run GATE=registry-publish-plan CI_ARTIFACTS="--artifact registry-plan=.artifacts/registry-publish/plan.json"

ci-registry-publish-execute:
	$(MAKE) ci-run GATE=registry-publish-execute CI_ARTIFACTS="--artifact registry-receipt=.artifacts/registry-publish/receipt.json"

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .
