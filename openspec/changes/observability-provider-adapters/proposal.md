## Source Links

- Product-Spec.md: `REQ-015: Observability 转换层`、`REQ-016: Eval Gate 与 trace/eval 闭环` 中的 provider score 写回边界、AI 护栏中的 secret 禁止写入 trace/eval/audit/local-jsonl。
- DEV-PLAN.md: `Phase 10: Observability Provider Adapters 与脱敏`、技术栈表中的 OpenTelemetry / Logfire / Phoenix / Langfuse、风险表中的 provider workflow 差异。
- API-Contract.md: 第 2 节 Observability / Eval Gate 边界、第 4.7 节数据保护、第 11 节入口 / 调用方映射。本 change 不新增 HTTP endpoint。
- Design-Brief.md 或设计稿：不适用，本 change 不涉及产品化前端 UI。
- CONTEXT.md / ADR: 未发现本轮必须读取的领域上下文或 ADR。

## Why

Phase 4 已有 CanonicalEvent、local JSONL sink 和 OTel mapping facade，但还没有稳定的 TelemetryFacade、provider adapter contract 或 provider 前 redaction 边界。Phase 10 需要让本地证据永远可用，并把 Logfire、Phoenix、Langfuse 等外部观测能力隔离在 adapter 层，否则 Phase 11 的 eval score 写回会继续污染业务入口和公共 DTO。

## What Changes

- 新增 `TelemetryFacade` 能力，统一接收 CanonicalEvent、trace/span context、provider-neutral telemetry payload，并始终先写 local/jsonl evidence。
- 新增 OTel exporter contract，使用 OpenTelemetry Python 的 TracerProvider / BatchSpanProcessor / OTLP HTTP exporter 边界，并覆盖 span、metric、event 输出映射；PyPI current 为 1.43.0，但 provider extra 因 Logfire 4.37.0 约束先锁可解析的 1.42.1 SDK/exporter 组合；exporter SDK 限定在 adapter 或 observability 集成层。
- 新增 Logfire、Phoenix、Langfuse adapter contract；adapter 可使用当前 provider SDK 入口，但业务 agent、template app、eval runner 和 core contracts 不直接 import provider SDK。
- 新增 observability trace context DTO，统一传播 trace/span/tenant/user/agent/run/session/tool/model/eval 关联字段。
- 新增 provider 前 redaction 规则，确保 secret 不进入 trace、eval、audit、local/jsonl、错误栈或 provider payload。
- 扩展 local/service profile 的 observability 配置入口和 doctor 输出，外部 provider 失败时本地证据不丢。
- 同步当前 PyPI 核验版本：OpenTelemetry current 1.43.0、Logfire 4.37.0、Arize Phoenix 17.21.0、Langfuse 4.13.2；依赖锁定记录 Logfire 对 OTel SDK `<1.43.0` 的约束。

## Non-Goals

- 不实现 Phase 11 `EvalCaseFactory`、eval draft/approve/list/run CLI/API、ReviewDatasetAdapter 或 approved dataset 写入流程。
- 不新增 eval HTTP endpoint、observability HTTP endpoint 或 provider webhook。
- 不接入 provider-native dataset/workflow 深集成；Logfire/Phoenix/Langfuse 的 dataset、annotation、experiment、score 高级工作流留给 Phase 11 或 P1。
- 不物理拆分独立 event/observability pipeline 服务，只保留可拆 adapter/facade/interface 边界。
- 不把 provider 原始 trace、SDK client、transport object 或未脱敏异常暴露为公共 API 契约。

## Capabilities

### New Capabilities

- `observability-provider-adapters`: TelemetryFacade、trace context、local/jsonl fallback、OTel/exporter contract、Logfire/Phoenix/Langfuse adapter contract、provider 前脱敏和配置/doctor 边界。

### Modified Capabilities

- 无。

## Impact

- 代码：新增或扩展 `agent_harness.observability`、`agent_harness.adapters.observability`、config schema、doctor diagnostics、provider import boundary tests。
- 契约：新增 OpenSpec delta spec 和 Phase 10 contract tests；不修改 HTTP route。
- 配置：更新 local/service profile 的 observability provider 配置示例，默认仍为 local/jsonl。
- 依赖：锁定当前 OTel / Logfire / Phoenix / Langfuse 版本或 optional extra；provider SDK 只允许出现在 adapter/integration seam。
- 安全：所有 telemetry payload 在写入 local/jsonl 和 provider 前统一 redaction；provider failure 只能产生脱敏错误摘要。
