## Context

当前 `ModelResponse`/MOD-005 已分别冻结文本与结构化业务结果，`ToolRegistry.call()` 则会在校验和 policy 后直接执行 handler。代码中不存在一个只把模型输出变成受信工具意图、但绝不执行工具的公共 seam。`ContextAssembler`、usage/shared-budget、CanonicalEvent 与 Agent descriptor 已提供后续阶段需要的来源、预算、身份和 evidence 基础；本 change 只补二者之间的认知边界。

三个 Phase 20 change 共用模型 turn、tool catalog、loop identity 和 replay 语义，但实现顺序固定为 20A → 20B → 20C。本设计只拥有 20A 文件；20B/20C 不得提前把执行、approval 或 durable loop state 塞入本 change。

## Goals / Non-Goals

**Goals:**
- 从公共 bound model seam 得到互斥的 final/tool intent 结果，不泄漏 SDK 类型。
- 用 Agent descriptor 与 Registry descriptor 的受信交集冻结只读 tool catalog。
- 为 provider 工具定义冻结独立 request shape、canonical bytes、字节上限与可信预算身份。
- 让 Registry 在零工具副作用下解析、授权并校验 intent，再返回不含 handler 的 DTO。
- 保留每个 model attempt 的既有 usage、预算、Policy/HITL 和 replay 语义。

**Non-Goals:**
- 不执行工具、不创建工具执行 approval/tool claim、不组装下一轮 context；既有 `model.invoke` approval/checkpoint/continuation 继续服务原模型调用。
- 不增加 tool-call streaming、HTTP route、数据库迁移或真实 provider smoke。
- 不修改 Phase 18/19 text、stream、route-chain、structured output 的既有结果形状。

## Decisions

### D1. 用外层判别联合，不把 tool intent 塞进 `ModelResponse.output_text`

新增 `ModelTurnResult`，以 `kind=final_text|final_structured|tool_intent` 封闭三种公共结果。Capability 再收窄合法分支：tool-intent protocol 只允许 final text/tool intent，structured 只允许 final structured，既有 text 只允许 final text；tool 分支使用独立 `ToolIntent`。这样既不把空文本或任意 JSON 当工具调用，也不把 `ModelResponse.output_text` 改成 nullable，且不要求一个 deployment 同时声明 tool 与 structured capability。

备选是给 `ModelResponse` 增加 nullable `tool_intent`，但会产生 output text、structured result 与 tool intent 多字段组合爆炸，并迫使所有旧 caller 理解新判别，拒绝。

### D2. Adapter 只返回未验证 candidate，核心分配所有耐久 identity

`ProviderToolIntentCandidate` 只含 provider/model、tool name、JSON object arguments、被提供 schema identity 和单轮 attempt/usage。`loop_id`、`turn_ordinal`、`tool_call_id`、arguments/catalog digest 均由核心从绑定运行上下文与 canonical JSON 计算。Adapter 不获得 ToolRegistry、handler、PolicyEngine 或 approval service。

Pydantic AI adapter 可以读取 vendor tool-call proposal，但构造 Agent 时不得注册 executable tool callback；若锁定版本无法只观察 proposal 而不自动执行，则该 provider capability fail closed，不能退化为 prompt 猜 JSON。Fake adapter 使用显式脚本覆盖 final/tool 分支。

### D3. Tool catalog 是 Agent 授权与 Registry 事实的有序交集

`tool-catalog-v1` 从 Agent 配置的 `tool_allowlist` 装载为只读 descriptor `tool_policy.allowed_tools`，再按该投影顺序映射 Registry descriptor：name、input schema ref/version/digest、action/resource、ordinal。两者必须逐值相同，不新增 `allowed_tools` 顶层配置字段。Loader 在 import executor/client 前拒绝未知、重复、schema 非 canonical 或 allowlist 漂移。Runtime 为 loop 冻结 catalog digest；request 只允许保序删减。

公开缩权不改写现有 `ModelRequest(extra=forbid)`。`BoundModelInvocationService.complete_tool_intent()` 使用独立 exact `ToolCatalogSelection(tool_names: tuple[str, ...])` 关键字参数：参数本身缺省为完整绑定 catalog，显式空 tuple 为合法空 catalog，非空值只能是 descriptor 列表的唯一保序子序列。核心校验后才投影连续 provider ordinal；未知、重复、重排、额外字段或任意 dict 输入在 claim/client/provider 前以 `model.tool_catalog_conflict` 关闭失败。这样将“没有提交选择”与“明确不提供工具”区分开，也不在通用文本请求 DTO 中暗藏 capability 专属字段。

备选是每轮调用 `list_tools()` 并让 provider 自选当前结果，但 reload 会改变在途身份，也让请求扩大授权，拒绝。

### D4. 工具定义使用独立请求形态并完整进入输入预算

`tool_intent` route 只接受 `single-user-text-with-tool-catalog/v1`。首个真实 deployment 固定为 singleton capability、legacy 单 route、`max_attempts=1`、空 fallback/classifier、structured repair 0；避免同一 model catalog ref 被解释成两个 request shape，也不把 Phase 18.2 route-chain 或 Phase 19 repair 混入本 change。核心把选定 `tool-catalog-v1` 投影成 `provider-tool-catalog-v1` exact object；工具项只含 name、schema ref/version/digest、strict canonical schema definition 与 ordinal，使用 UTF-8、`ensure_ascii=false`、排序键、紧凑 separators、显式 null、有限 JSON 值形成唯一 bytes。Adapter 只映射这份快照，不从 SDK 或 current Registry 补值。

该 shape 使用 `model-catalog/v2` 的非负 `max_tool_catalog_utf8_bytes`。实际 catalog bytes 先过上限，再按 `prompt bytes + catalog bytes + input_envelope_token_bound` 形成可信输入；静态 ceiling 使用 catalog max，动态 reservation 使用实际 bytes。`tool-intent-request-identity-v1` exact object 冻结 request shape、model/tool catalog digest、actual/max bytes、trusted input bound 与 output cap；私有 route/budget snapshot 保存完整 provider catalog bytes，公开 evidence 只保存 identity/digest。Approval arguments/continuation 绑定该 digest；recovery 只读耐久 snapshot，不从 current Registry 重建。No-tools shape、超长 catalog、预算不足或恢复漂移全部在 provider 前零调用拒绝。

### D5. Registry resolve 与执行分离，执行仍做防御性重验

增加只读 `resolve_intent()`，返回 `ResolvedToolIntent`，不暴露 handler/callable，不运行 preflight/policy，不创建 artifact/tool invocation，也不发 MCP/shell/file/network。它可以写脱敏 validation audit，但零工具副作用必须由公共计数器合同证明。20B 的 `call`/`call_approved` 必须重新校验同一 schema/catalog/arguments 绑定，关闭 resolve→execute 的 TOCTOU。

### D6. 每个 intent model turn 复用既有调用生命周期

工具意图仍是一次真实 model request：route/capability、`model.invoke` soft policy、shared-budget reservation、attempt、usage evidence、cleanup、unknown 和 exact replay 全部复用既有模型调用规则。Policy `deny`保持零provider调用；`require_approval`复用既有durable approval/checkpoint和approved continuation，逐值保留原usage/operation/request/route/catalog/turn identity，批准后最多调用provider一次。该模型approval只授权原model turn；即使恢复后得到合法`ToolIntent`，工具handler、工具执行approval、tool claim与`tool.call.*`仍为零。工具是否执行不影响已经发生的model token/cost。新capability名为`tool_intent`，必须由deployment、route与provider protocol同时支持。

## Affected Surfaces

- 公共 DTO/协议：候选与 exact `ToolCatalogSelection` 新增至 `packages/agent-harness/src/agent_harness/models/tool_intent.py`；窄更新 `models/{__init__,providers,invocation,usage}.py`，保持 `ModelRequest` 不变。
- Registry/catalog：`registry/{descriptor,_loader,registry}.py`、`tools/{types,registry}.py`。
- Composition：`runtime/services.py` 只注入只读 descriptor/catalog resolver，不增加第二个可变 registry。
- Adapter：`adapters/models/{fake,pydantic_ai}.py` 与私有 `_pydantic_ai_tool_intent.py`；vendor import 仍限 adapter。
- 配置：`config/model_catalog.py` 增加 `model-catalog/v2`、tool-enabled request shape/catalog byte cap，deployment capability 与 Agent model/tool policy exact loader；不新增环境变量或依赖。
- 测试：公共 bound seam、Registry resolve、两个 provider id doubles、Pydantic AI 零 callback/零工具执行、usage/budget/replay、text/stream/structured 兼容。
- 文档：Product/API/DEV、adapter/extension/building guides 与 living plan；不新增 UI/HTTP 文档。

20A 生产、测试和文档由一个 worktree/owner 串行写入。20B/20C 只能读取冻结结果；不得并行修改上述共享 DTO/Registry/plan 文件。

## Testing Seams

- `build_execution_context()` 取得的绑定 model turn seam：tool-intent capability 的 final text、tool intent 和非法 mixed/cross-capability 结果；structured result 只由独立 structured seam 验证。
- `ToolCatalogSelection`：缺省完整、显式空、唯一保序子集，以及未知/重复/重排/额外字段/塞入 `ModelRequest` 的零副作用拒绝。
- `ToolRegistry.resolve_intent()`：未知/未授权/schema/source/catalog/arguments 正负路径，所有 handler/MCP/shell/file/network 计数为零。
- 两个不同 provider id 的 `ModelToolIntentProvider` doubles：candidate exact shape、SDK object 拒绝、provider/model/schema 漂移、usage/cleanup/unknown。
- Tool request 合同：canonical catalog golden vector、no-tools/with-tools shape mismatch、超长 schema/catalog、预算不足、approval/replay identity 与恢复 catalog 漂移，失败路径 provider/client 计数为零。
- 既有 `model.invoke` Policy/HITL：allow/deny/require-approval waiting、matching approved continuation、identity篡改和crash replay；批准最多触发一次provider，所有工具副作用仍为零。
- Pydantic AI adapter contract：没有 executable callback，provider-native tool runtime count 为零；不支持只观察 proposal 时 capability fail closed。
- 既有 text/stream/route-chain/structured、ToolRegistry CLI/call 与 fake eval 回归。

## Risks / Trade-offs

- [Pydantic AI 锁定版本可能默认执行注册工具] → 不向 SDK 注册 handler；只允许可观察的 proposal seam，否则该 capability 明确 unsupported。
- [Catalog reload 造成在途漂移] → loop 开始冻结 canonical catalog digest，恢复只接受耐久 snapshot；reload 只影响新 loop。
- [工具 schema 位于旧 prompt 预算之外] → 独立 request shape 与 model-catalog v2 把 canonical catalog actual/max bytes 同时纳入静态 ceiling、动态 reservation 和耐久 evidence。
- [把已有 `ModelDecision.action` 误当工具判别] → 新判别联合独立存在；旧 routing decision 继续只描述 route/fallback/policy。
- [DTO/validator 文件变厚] → 新 tool intent DTO/纯 canonical 逻辑独立文件；公共 façade 只窄委托，所有 Python 文件遵守有效代码行门禁。

## Migration Plan

1. 先增加公共 red contracts，证明旧 bound seam 无 tool intent、Registry 无只读 resolve、Pydantic adapter 不能泄漏 SDK/执行工具。
2. 增加 DTO/catalog/model-catalog v2 loader 与 canonical provider request，再接 bound invocation、fake provider 和 Pydantic normalization。
3. 接 usage/evidence/replay 与文档，重跑 Phase 18/19/工具兼容门禁。
4. 本 change 不迁移数据库。回滚删除新 capability/DTO/loader 字段即可；已耐久的未知新 DTO 不允许旧 binary 猜测解释。

## Open Questions

- 无阻断性问题。若锁定 Pydantic AI 无法在零 callback 下取得 tool proposal，首个实现只交付 fake/provider protocol 与明确 unsupported 的 Pydantic 路径；不得为通过测试而注册空 handler或把 JSON 文本猜成 intent。
