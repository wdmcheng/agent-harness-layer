## Context

Phase 4 已提供 `CanonicalEvent`、`EventBus`、`LocalJsonlEventSink`、artifact store、基础 redaction 和 `map_event_to_otel()`。Phase 7/8/9 已让 audit、tool/MCP、retrieval 进入同一套 tenant/run/agent/trace 证据链。当前缺口是 Phase 10 指定的 provider adapter contract：系统还没有一个 facade 能保证 provider 失败时 local evidence 不丢，也没有 provider 前 redaction 和 trace/span context propagation 的统一入口。

外部资料核验结论：
- OpenTelemetry Python 当前 PyPI 版本为 1.43.0；官方文档使用 `TracerProvider`、`BatchSpanProcessor` 和 `OTLPSpanExporter` 作为 OTLP HTTP trace export 基础。Logfire 4.37.0 当前约束 `opentelemetry-sdk<1.43.0`，因此 Phase 10 optional extra 先锁可解析的 OTel SDK/exporter 1.42.1 组合。
- Logfire 当前 PyPI 版本为 4.37.0；官方资料显示既可用 `logfire.configure()`，也可通过 OTel OTLP exporter 发送 trace。
- Arize Phoenix 当前 PyPI 版本为 17.21.0；官方资料显示 `phoenix.otel.register()` 可注册 tracer provider，并可接收 OTLP trace。
- Langfuse 当前 PyPI 版本为 4.13.2；v4 Python SDK 使用 observation-centric tracing，官方资料显示 `get_client()`、context manager/decorator 和 score API 是当前入口。

## Goals / Non-Goals

**Goals:**
- 用 `TelemetryFacade` 统一 telemetry publish / provider fan-out / local fallback / redaction。
- 用 provider-neutral DTO 表达 trace/span/context/payload，不把 provider SDK object 泄漏到 core、template 或业务 agent。
- 让 local/jsonl 在未配置 SaaS provider 或 provider 失败时仍产出完整本地证据。
- 为 OTel、Logfire、Phoenix、Langfuse 提供 contract-testable adapter seam。
- 把 trace/span/tenant/user/agent/run/session/tool/model/eval 关联字段统一压进 telemetry payload。

**Non-Goals:**
- 不实现 eval draft、review queue、approved dataset、EvalRunner 或 ScoreSink 完整流程。
- 不做 provider-native dataset/annotation/experiment 高级工作流。
- 不新增 HTTP endpoint。

## Decisions

1. **TelemetryFacade 先写 local，再 fan-out provider。**
   Facade 接收 CanonicalEvent 或 provider-neutral telemetry payload，先经 redaction 写入 local sink，再尝试外部 provider adapter。替代方案是只在 provider 成功后写本地证据；拒绝，因为外部 provider 失败会丢审计证据。

2. **Provider adapter 只接受脱敏 DTO。**
   adapter 输入是 `TelemetryRecord` / `TelemetryContext`，包含稳定关联字段和 payload/ref；adapter 不接收原始 exception、SDK response 或业务对象。替代方案是在每个 adapter 里各自脱敏；拒绝，因为会让 secret fixture 在 provider 差异下漏出。

3. **OTel exporter 作为 provider contract，不污染 CanonicalEvent core。**
   `observability.otel` 保持 mapping facade；真实 exporter 初始化放在 adapter/integration seam。Phase 10 的 adapter contract 覆盖 span、metric 和 event 三类输出的稳定字段映射。这样 core event modules 不需要 import exporter SDK，provider import boundary 仍可扫描。

4. **Logfire/Phoenix/Langfuse adapter contract 先证明边界，不承诺深工作流。**
   Phase 10 只需要 trace/event payload 可发送、失败可降级、SDK import 不越界。dataset、score、annotation 的 provider-native 语义放 Phase 11 或 P1，否则会把 Eval Gate 提前塞进本 change。

5. **配置保留 local/jsonl 默认，service profile 只提供 provider 入口。**
   local profile 不需要 SaaS key。service profile 可配置 OTel endpoint/provider kinds，但 provider 缺凭据时 doctor 给出可操作状态，不让 smoke 依赖外部账号。

## Affected Surfaces

- `packages/agent-harness/src/agent_harness/observability/facade.py`
- `packages/agent-harness/src/agent_harness/observability/context.py`
- `packages/agent-harness/src/agent_harness/observability/redaction.py`
- `packages/agent-harness/src/agent_harness/adapters/observability/otel.py`
- `packages/agent-harness/src/agent_harness/adapters/observability/logfire.py`
- `packages/agent-harness/src/agent_harness/adapters/observability/phoenix.py`
- `packages/agent-harness/src/agent_harness/adapters/observability/langfuse.py`
- `packages/agent-harness/src/agent_harness/config/schemas.py`
- `packages/agent-harness/src/agent_harness/storage/diagnostics.py`
- `templates/service-app/configs/profiles/local.yaml`
- `templates/service-app/configs/profiles/service.yaml`
- `tests/contracts/` 中 Phase 10 contract、provider import boundary、doctor/config/redaction/local evidence/fallback tests

## Testing Seams

- Module seam: `TelemetryFacade.publish_event()` / `publish_record()` 使用 fake provider adapter，验证 local-first、provider failure fallback 和脱敏 payload。
- Adapter seam: OTel/Logfire/Phoenix/Langfuse adapter contract tests 使用 fake client/module，不需要真实 API key 或 SaaS 账号；OTel seam 需覆盖 span、metric、event 输出。
- Config seam: local/service profile 加载并验证 observability provider 配置；local 默认不要求 provider key。
- Doctor seam: `agent-harness doctor --profile local/service` 输出 local sink 和 provider 配置状态，不因 optional provider 缺失错误退出。
- Import boundary seam: 扫描 business/template/example surfaces，证明 provider SDK 不越过 adapter/integration path。
- Redaction seam: secret fixture 同时穿过 event/local/jsonl/audit/eval-like payload/provider payload/error summary，断言无原始 secret。

## Risks / Trade-offs

- [Risk] Provider SDK API 在短期内漂移。→ 缓解：版本由 PyPI 当前信息锁定，adapter 输入保持 provider-neutral，contract tests 用 fake client 锁住本仓库边界。
- [Risk] 外部 provider 失败被误认为 run 失败。→ 缓解：facade 记录脱敏 provider error summary，但不改变业务 run terminal 状态。
- [Risk] redaction 重复实现导致漏网。→ 缓解：provider 前统一 `redact_telemetry_payload()`，adapter 不接收未脱敏 payload。
- [Risk] Phase 10 悄悄实现 Phase 11 eval workflow。→ 缓解：本 change 只保留 eval correlation 字段和 provider payload type，不创建 eval case/review/run API。

## Migration Plan

本 change 不新增数据库表。配置向后兼容：现有 `observability.kind: local-jsonl` 继续有效；service profile 新增 provider 配置入口时默认关闭外部发送。回滚时移除 provider 配置和 adapter 模块，local/jsonl evidence sink 保持不变。

## Open Questions

- Phase 11 是否用同一 `TelemetryFacade` 承接 `ScoreSink` 写回 provider；本 change 只预留 eval correlation 字段，不实现 score workflow。
