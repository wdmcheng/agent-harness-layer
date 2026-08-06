## Source Links

- Product-Spec.md：REQ-010、REQ-011、REQ-012、REQ-024、REQ-025、REQ-028、REQ-029 / AC-104～AC-105、AC-111
- API-Contract.md：5.9 `CanonicalEvent`、5.19～5.21 工具 DTO/错误码、TLS-001～TLS-003、MOD-002、MOD-005、MOD-006
- DEV-PLAN.md：Phase 20 / 20A provider-neutral tool intent
- docs/plans/architecture-evolution-plan.md：7.6 Phase 20
- docs/plans/architecture-evolution-change-matrix.md：20A 与 5.4 Phase 19 → 20
- ADR：docs/adr/0002-vendor-adapter-isolation.zh-CN.md

## Why

现有 model seam 只能返回文本或结构化业务结果，现有 `ToolRegistry.call()` 则直接进入 policy/handler；两者之间缺少一个“模型只提议、核心只校验、绝不执行”的 provider-neutral 边界。Phase 20A 必须先冻结这条边界，才能防止 provider-native tool runtime、任意 JSON 猜测和 SDK tool object 越过 Harness 授权面。

## What Changes

- 增加 `ModelTurnResult` 判别联合、adapter→core 的 `ProviderToolIntentCandidate` 和核心 `ToolIntent` exact DTO；最终文本、最终结构化业务结果与工具意图互斥。
- 增加独立 exact `ToolCatalogSelection` 作为 bound tool-intent seam 的关键字参数；缺省使用完整绑定 catalog，显式空列表与唯一保序子集只缩权，不扩展 `ModelRequest`。
- 从绑定 Agent descriptor 与 Registry 只读 descriptor 冻结 `tool-catalog-v1`；request 只能缩小有序工具集合，provider 不能决定 schema、allowlist、action/resource 或稳定 identity。
- 新增唯一工具启用请求形态 `single-user-text-with-tool-catalog/v1` 与 `model-catalog/v2` 工具目录字节上限；canonical provider schema bytes 进入可信输入、预算、route/snapshot/operation、approval、usage evidence 与 replay identity，no-tools shape 不得携带工具。
- 首个真实 `tool_intent` deployment 是 singleton capability、单 route、单 attempt，不声明 fallback/classifier/structured repair；其 protocol 只允许最终文本或工具意图，结构化业务结果仍由独立 capability 拥有。
- 为 `ToolRegistry` 增加无工具副作用的 resolve/validation seam，校验名称、Agent allowlist、catalog/schema/source identity 和 arguments，返回不含 callable/handler 的 `ResolvedToolIntent`。
- 为 fake 与 Pydantic AI adapter 增加只归一化 intent 的 provider-neutral seam；adapter 不注册 executable callback，不执行工具，不暴露 SDK object。
- 让工具意图 model turn 复用既有 route、Policy/HITL、usage/shared-budget、attempt/evidence 和 replay 边界；本 change 不执行工具，也不产生 `tool.call.started`。

## Non-Goals

- 不执行 File/Shell/MCP 或任意本地/外部工具，不创建、解析或恢复工具执行 approval，也不形成多轮循环；既有 `model.invoke` Policy/HITL waiting 与 approved continuation 必须保持兼容，它只授权原模型调用，不授权工具。
- 不做 tool-call streaming/args delta、structured streaming、跨 provider structured fallback 或真实 provider 调用。
- 不修改 HTTP route，不引入通用状态机、后台 scheduler、动态插件 marketplace 或 Phase 21 重构。

## Capabilities

### New Capabilities

- `provider-neutral-tool-intent`：冻结单轮工具意图、catalog、核心校验、adapter 隔离和零执行语义。

### Modified Capabilities

- `agent-registry-model-context`：Agent 工具 catalog 与绑定 model turn 只能从 Registry/descriptor 的受信交集产生。
- `provider-neutral-structured-output`：工具意图与结构化业务输出必须使用不同判别类型，不能互相伪装。
- `tool-execution-boundaries`：增加无副作用 resolve seam，并要求执行 seam 防御性重验已解析 intent。
- `model-usage-evidence`：工具意图 model turn 的全部 provider attempts 进入既有 usage/evidence/replay，不因“未执行工具”而记零。
- `typed-config`：model catalog v2 为 tool-enabled request 冻结目录字节上限、request shape 与 canonical digest，既有 no-tools v1 行为保持不变。

## Impact

- 预计影响 `models/` 的 provider-neutral turn/intent DTO 与 invocation seam、`config/model_catalog.py` 的 v2 request shape、`registry/` descriptor/catalog、`tools/registry.py` 的只读解析、`runtime/services.py` 的绑定 capability、`adapters/models/{fake,pydantic_ai}` 的 intent normalization，以及对应 contract/eval/双语维护文档。
- 不要求数据库迁移；若 red contract 证明现有 usage/evidence schema 无法表达 intent turn，必须先修订本契约并重审，不能现场加列。
- 本 change 与 `policy-gated-tool-loop`、`durable-tool-loop-resume` 共享 DTO、CanonicalEvent 和 replay 验收，必须串行实现并联合审查。
