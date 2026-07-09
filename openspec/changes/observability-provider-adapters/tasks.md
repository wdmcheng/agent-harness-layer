## 1. 契约与测试先行

- [x] 1.1 新增 Phase 10 OpenSpec / contract tests，覆盖 `observability-provider-adapters` spec、TelemetryFacade local-first、trace context 字段、OTel span/metric/event mapping、provider failure fallback、redaction fixture 和 provider import boundary。
- [x] 1.2 更新 config / doctor contract tests，先让 local/service profile 的 observability provider 配置、当前 provider 版本和降级语义断言失败。

## 2. TelemetryFacade、trace context 与 redaction

- [x] 2.1 新增 `agent_harness.observability.context`，公开 `TelemetryContext`、span/context helper 和关联字段 merge 规则。
- [x] 2.2 新增 `agent_harness.observability.redaction`，统一 provider 前 redaction，并覆盖 secret fixture、error summary 和 eval/audit-like payload。
- [x] 2.3 新增 `agent_harness.observability.facade`，实现 local/jsonl first、provider fan-out、provider failure degraded status 和 CanonicalEvent/telemetry record 公开 seam。

## 3. OTel 与 provider adapters

- [x] 3.1 新增 OTel exporter adapter contract，使用 OpenTelemetry TracerProvider / BatchSpanProcessor / OTLP HTTP exporter 边界生成稳定 span/metric/event attributes，失败时不影响 local evidence。
- [x] 3.2 新增 Logfire adapter contract，provider SDK import 仅在 adapter/integration path，contract tests 使用 fake client。
- [x] 3.3 新增 Phoenix adapter contract，provider SDK import 仅在 adapter/integration path，contract tests 使用 fake client。
- [x] 3.4 新增 Langfuse adapter contract，provider SDK import 仅在 adapter/integration path，contract tests 使用 fake client。

## 4. 配置、doctor、版本锁定与模板

- [x] 4.1 更新 `pyproject.toml` / `uv.lock` 或 optional extras，锁定当前 OTel / Logfire / Phoenix / Langfuse 版本，并保留 provider SDK 不污染业务入口。
- [x] 4.2 扩展 config schema 与 local/service profile，local 默认 local/jsonl，service 提供 OTel/provider 配置入口和注释说明。
- [x] 4.3 扩展 doctor observability diagnostics，输出 local sink、provider 配置、optional provider 缺失/不可达的脱敏降级提示。

## 5. 收口验证

- [x] 5.1 跑 `openspec validate observability-provider-adapters --type change --strict`、Phase 10 targeted tests、`uv run pytest`、`make quality`、`uv run openspec validate --all --strict`、`uv run python scripts/smoke_local.py`、`make smoke-service`、`make build`、`make license-check`、`uv run pre-commit run --all-files`。
