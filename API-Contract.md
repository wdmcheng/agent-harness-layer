# API Contract: Agent Harness Layer

> 本文件定义 Agent Harness Layer 当前版本的 HTTP API 与跨入口契约。
> 本项目当前版本不做产品化前端 UI，因此这里不使用“页面到接口映射”，而使用“入口 / 调用方到接口映射”。
> 如果本文档、`Product-Spec.md`、`DEV-PLAN.md`、OpenSpec 和运行时 OpenAPI 不一致：先按 `Product-Spec.md` 判断范围，再更新本文档，最后让实现、测试和 OpenAPI 对齐。

---

## 0. 契约目标

- 固定 service-app 的 `/api/v1/...` 字段级契约，让 HTTP API、CLI、worker 和未来可拆进程共享同一套边界语言。
- 把架构图中的 Access、Runtime、Engine、Tools、Infra 五层边界落到可检查的 DTO、CanonicalEvent、error envelope 和 route contract。
- 明确当前已实现 run lifecycle API 与后续保留 API 的边界，避免把规划中能力写成已交付能力。
- 让每个新增或修改 endpoint 的验收都包含局部 OpenAPI 漂移检查，不把契约问题攒到发布前。
- 保持 Product Spec 的约束：当前版本只提供 API、CLI、OpenAPI/Swagger/Redoc，不做完整 SaaS 管理台。

## 1. 上游依据

| 输入 | 用途 |
|---|---|
| `Product-Spec.md` | 产品范围、当前版本 API 列表、身份/权限/HITL、CanonicalEvent、未来拆分边界。 |
| `DEV-PLAN.md` | 开发计划顺序、已完成能力、后续 endpoint 所属计划项和验收门禁。 |
| `docs/architecture/pydantic-ai-agent-architecture.drawio` | 5 层运行中轴、SSE/WS 回边、HITL 回路、信任边界、部署拆分边界。 |
| `openspec/specs/core-contracts/spec.md` | DTO serialization、typed error envelope、trust/source/context refs 的稳定公共契约。 |
| `openspec/specs/runtime-checkpoint-runs/spec.md` | API、CLI、worker 共用 `RunOrchestrator` seam 的行为契约。 |
| `templates/service-app/app/api/routes/runs.py` | 当前已实现 run API route、请求/响应 DTO 和事件读取 seam。 |
| `templates/service-app/app/main.py` | FastAPI app factory、router 注册和统一错误 envelope handler。 |
| `tests/contracts/test_runtime_checkpoint_runs_contracts.py` | 当前 OpenAPI route registration、error envelope、event filtering 和 worker seam 验证。 |

## 2. 架构映射

| 架构层 | API 契约约束 |
|---|---|
| Access | FastAPI route 只做协议适配、认证注入、请求/响应转换和 OpenAPI 暴露，不直接操作 ORM、DBOS、provider SDK 或业务 agent。 |
| Runtime | run 创建、取消、resume、checkpoint、idempotency 和 event 写入只通过 `RunOrchestrator` 及其公开 DTO/Protocol seam。 |
| Engine | 模型、上下文组装、预算和 fallback 不泄漏 provider 原始对象；进入 API 的内容必须先转换为稳定 DTO 或 `CanonicalEvent`。 |
| Tools | Tool/MCP/Retrieval output 默认按不可信输入处理，进入 API/event 前必须带 `source_ref`、`trust_level`、截断信息或 `artifact_ref`。 |
| Infra | Storage、queue、event sink、observability provider 只通过 repository/provider/facade 交换；API 不暴露 session、engine、vendor handle。 |
| Eval Gate | eval draft/approved/run/score API 必须保留人工审核、secret 脱敏和 trace/eval 关联字段。 |
| Observability | API、worker、tool/model gateway 拆分后仍必须携带适用的 `request_id`、`trace_id`、`tenant_id`、`agent_id`、`run_id`。 |
| 部署边界 | 当前版本可以同进程运行，但 API/worker/model/tool/storage/event pipeline 的跨边界数据必须是 Pydantic DTO、`CanonicalEvent` 或明确 interface。 |

## 3. 接口文档规范

每个 HTTP endpoint 必须包含以下字段：

| 字段 | 必填 | 说明 |
|---|---:|---|
| Contract ID | Yes | 稳定编号，例如 `RUN-001`、`AGT-001`。 |
| 状态 | Yes | `已实现`、`规划中`、`保留路径`。不能把规划中能力写成已交付。 |
| 入口 / 调用方 | Yes | CLI 等价入口、OpenAPI 调用方、worker、service-app、未来 gateway。 |
| 用途 | Yes | 一句话说明调用方完成什么。 |
| 方法 | Yes | `GET` / `POST` / `PUT` / `PATCH` / `DELETE`。 |
| 路径 | Yes | 稳定 `/api/v1/...` 路径。 |
| 认证 | Yes | 当前 local/template 行为和目标认证都要写清。 |
| 请求头 | Yes | 必填和可选 header。 |
| Path 参数 | Conditional | 路径参数名、类型、校验。 |
| URL 参数 | Conditional | query 参数名、类型、默认值、过滤和可见性语义。 |
| 请求体 | Conditional | JSON / multipart / none；必须引用 schema。 |
| 幂等性 | Yes | 重复请求如何处理。 |
| 副作用 | Yes | 是否写库、写文件、创建 run、触发 worker、调用 provider。 |
| 成功响应码 | Yes | 当前实现和目标状态码不一致时必须写明。 |
| 响应头 | Yes | 当前实现保证什么；未来增强不能伪装成当前已实现。 |
| 响应体 | Conditional | 必须引用 schema；无 body 写 `none`。 |
| 错误响应码 | Yes | 该 endpoint 可能返回的业务错误和统一 error code。 |
| 状态语义 | Yes | loading / empty / error / disabled / success 如何由调用方判断。 |
| 安全规则 | Yes | secret、权限、日志脱敏、可见性过滤、数据保护、破坏性行为。 |
| 验证要求 | Yes | 对应 contract test、OpenAPI path/schema 检查或 smoke 证据。 |

## 4. 通用约定

### 4.1 Base URL 与版本

| 环境 | API |
|---|---|
| local/service template | `http://localhost:<port>`，具体端口由启动命令或 Uvicorn 配置决定。 |
| OpenAPI | FastAPI 默认 `/openapi.json`、Swagger `/docs`、Redoc `/redoc`。 |

规则：

- 所有当前版本 HTTP API 使用 `/api/v1/...`。
- 破坏性 API 变更必须进入 `/api/v2`，不得在 `/api/v1` 静默改字段含义。
- 本项目尚处于 P0 预发布阶段；本版对既有 `budget.max_tokens_per_run` / `budget.max_cost_usd_per_run` 做一次显式且文档化的安全语义收紧：字段 shape、名称和类型不变，但约束对象由单个 run 改为该 root 的整个 parent execution tree。P0 首次发布后不得继续在 `/api/v1` 以同类理由改变语义，后续破坏性调整必须进入新 API 版本。
- 当前版本不定义完整 SaaS 前端 URL，也不提供登录、注册、组织邀请或计费页面。

### 4.2 认证与身份

当前状态：

- 已有 `IdentityContext` / `PermissionContext` 和 default tenant/user contract。
- 当前已实现 agents/run/policy/approval routes 已接入 FastAPI auth dependency；local/dev profile 未配置 verifier 时注入默认身份，service/API key profile 要求 Bearer/API key。

当前规则：

- 除明确的 health/local dev seam 外，mutating API 必须支持 API Key / Bearer Token。
- 认证层必须注入 `IdentityContext`，未启用多租户时使用默认 `tenant_id="default"`。
- 无效 token 调用受保护 API 必须返回认证错误且不创建 run、approval、eval case 或 audit side effect。
- `GET /api/v1/agents` 已接入认证和 tenant/identity 可见性过滤；该 route 不创建资源，但仍不得向未授权调用方暴露 descriptor。

### 4.3 通用请求头

| Header | 必填 | 说明 |
|---|---:|---|
| `Accept` | No | 默认 `application/json`；P0 待实现 RUN-006 SSE 使用 `text/event-stream`。 |
| `Content-Type` | Conditional | JSON mutating request 使用 `application/json`。 |
| `X-Request-Id` | No | 调用方可传；服务端没有收到时生成 UUID，并写入响应 body 的 `request_id`。 |
| `X-Trace-Id` | No | RUN-001 使用 provider-neutral normalizer：调用方值必须匹配 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`，不 trim、不折叠大小写；缺失时 runtime 在业务副作用前生成 lowercase RFC 4122 UUID canonical trace；非法格式返回 `422 validation_error`，已绑定其他 root run 返回 `409 trace.conflict`。同一 idempotency key 缺失 trace 或提供首次 canonical trace 时复用首次 run；提供不同 trace 返回 `409 trace.idempotency_conflict`。 |
| `Authorization` | Conditional | 认证 profile 启用 verifier 时必填 `Bearer <token>` 或等价 API key；local/dev profile 未配置 verifier 时由服务端注入默认身份。 |

### 4.4 通用响应格式

当前 service API 采用直接 DTO 响应，不包一层 `data/meta`：

```json
{
  "request_id": "req_...",
  "run_id": "run_...",
  "status": "completed"
}
```

硬约束：

- API response body 必须带 `request_id`；run 相关 response body 必须带 `run_id`。
- 当前实现不保证 response header 回写 `X-Request-Id`。后续若要加该 header，必须同步本文档和 OpenAPI drift tests。
- 时间字段使用 ISO 8601 UTC 字符串。
- ID 均为字符串，调用方不得依赖具体格式。
- DTO 使用 Pydantic v2 schema，未声明字段必须拒绝或不出现在公共 payload。

### 4.5 错误响应格式

统一错误 envelope：

```json
{
  "error": {
    "code": "api.not_found",
    "message": "run not found: run_123",
    "request_id": "req_...",
    "field_path": "optional.field",
    "hint": "optional remediation"
  }
}
```

规则：

- `code` 是稳定英文枚举，调用方用它做分支。
- `message` 面向开发者和 API 使用方，可直接展示在 CLI/OpenAPI 调试场景。
- `field_path` 和 `hint` 可选，主要用于配置、schema、认证或 validation diagnostics。
- 错误 envelope 不得包含 secret、token、cookie、provider 原始响应或完整大 payload。
- 当前 tests 已覆盖 404/500 走 `ApiErrorEnvelope`；Service App 基础表面及 Executor、approval continuation、scaffold 合入后的最终组合复扫均已覆盖所有适用 operation 的 422 `ApiErrorEnvelope`。

### 4.6 通用状态码

| 状态码 | 用途 |
|---:|---|
| 200 | 同步读取或同步操作成功；local profile 的 run create 及当前 cancel/resume 返回 200。 |
| 201 | 未来同步创建资源成功时可用；使用前必须更新本契约。 |
| 202 | 当前 service profile 的 RUN-001 durable enqueue 成功；必须返回 run id。其他 endpoint 使用前必须单独声明。 |
| 204 | 成功且无响应体。 |
| 400 | 请求语义错误或 HTTPException 400。 |
| 401 | 未认证或 token 无效；local/dev profile 未配置 verifier 时不要求 Authorization。 |
| 403 | 已认证但权限、policy 或可见性限制不通过。 |
| 404 | 资源不存在，或对当前身份不可见。 |
| 409 | run 状态冲突、非法 transition、重复资源或 approval 状态冲突。 |
| 422 | 字段校验失败；完整 HTTP API 必须统一成 `ApiErrorEnvelope`。 |
| 429 | 速率限制、预算、队列或并发上限。 |
| 500 | 未预期服务端错误，必须走 `api.internal_error` envelope。 |
| 503 | 数据库、worker、queue、model provider 或外部依赖暂不可用。 |

### 4.7 可见性与数据保护

- `reasoning.delta` 可以进入 internal evidence，但普通用户 event stream 默认不能看到。
- Tool/MCP/Retrieval output 默认不可信，进入 API/event 前必须保留来源、可信级别和截断摘要。
- 大 payload、tool output、trace evidence、eval evidence 默认走 `payload_ref` / `artifact_ref`，不要塞进 API 响应正文。
- Secret 不得进入 API body、error envelope、event payload、trace、eval case、audit log 或 local/jsonl。
- 破坏性动作、shell、workspace 外访问、写 approved eval dataset、修改 policy 默认必须经 policy/approval seam。

## 5. 通用 Schema

### 5.1 `ErrorDetail`

```json
{
  "code": "api.not_found",
  "message": "run not found: run_123",
  "request_id": "req_123",
  "field_path": "optional.path",
  "hint": "optional remediation"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `code` | string | Yes | 稳定错误码。 |
| `message` | string | Yes | 面向调用方的错误说明。 |
| `request_id` | string | No | 请求关联 ID。 |
| `field_path` | string | No | 字段路径，用于 validation/config/auth diagnostics。 |
| `hint` | string | No | 修复建议。 |

### 5.2 `ApiErrorEnvelope`

```json
{
  "error": {
    "code": "api.internal_error",
    "message": "boom",
    "request_id": "req_500"
  }
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `error` | `ErrorDetail` | Yes | 统一错误详情。 |

### 5.3 `RunStatus`

| 值 | 说明 | Terminal |
|---|---|---:|
| `created` | run record 已创建但未开始执行。 | No |
| `running` | run 正在执行。 | No |
| `waiting` | run 等待 checkpoint/resume 或 approval。 | No |
| `completed` | run 成功结束。 | Yes |
| `failed` | run 失败结束。 | Yes |
| `cancelled` | run 被取消。 | Yes |

### 5.4 `AgentRunCreateRequest`

```json
{
  "input": {
    "prompt": "hello"
  },
  "idempotency_key": "idem-123"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `input` | object | No | agent 输入 payload。默认 `{}`；进入 runtime 前必须经过 guardrail/trust 处理。 |
| `idempotency_key` | string | No | 同一 tenant/agent/session 下防重复提交。缺失时每次请求可创建新 run。 |

### 5.5 `RunCreateRequest`

内部 helper 使用的 request schema；HTTP `POST /agents/{agent_id}/runs` 不在 body 里重复传 `agent_id`。

```json
{
  "agent_id": "fake-agent",
  "input": {
    "prompt": "hello"
  },
  "idempotency_key": "idem-123"
}
```

### 5.6 `RunCreateResponse`

```json
{
  "request_id": "req_123",
  "run_id": "run_123",
  "status": "completed",
  "terminal_event": "run.completed"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `request_id` | string | Yes | API 请求关联 ID。 |
| `run_id` | string | Yes | run 稳定 ID。 |
| `status` | `RunStatus` | Yes | 当前 run 状态。 |
| `terminal_event` | string | No | terminal run event，例如 `run.completed`。非 terminal 状态可为空。 |

安全边界：`RunCreateResponse` 不返回 `resume_token`。恢复 token 只允许经 `RunResumeRequest` 输入，或由内部 approval service seam 持有；公共 response 和默认事件列表不得泄露 token。

### 5.7 `RunResumeRequest`

```json
{
  "resume_token": "resume_123"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `resume_token` | string | Yes | checkpoint resume token；必须属于 path 中的 `run_id`。 |

### 5.8 `RunEventsResponse`

```json
{
  "request_id": "req_123",
  "events": []
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `request_id` | string | Yes | API 请求关联 ID。 |
| `events` | `CanonicalEvent[]` | Yes | 按 `seq` 读取的事件列表。默认过滤 `reasoning.delta`。 |

### 5.9 `CanonicalEvent`

```json
{
  "event_id": "evt_123",
  "tenant_id": "default",
  "run_id": "run_123",
  "agent_id": "fake-agent",
  "event_type": "run.completed",
  "event_version": "1.0",
  "seq": 2,
  "timestamp": "2026-07-07T00:00:00Z",
  "trace_id": "trace_123",
  "record_scope": "run",
  "payload": {
    "status": "completed"
  },
  "terminal": true,
  "visibility": "public"
}
```

固定 `event_type` 目录（39 种，与 `CanonicalEventType` 精确相等）：

```text
run.queued
run.started
run.resumed
run.completed
run.failed
run.cancelled
model.request.started
model.output.delta
model.output.completed
model.structured.delta
model.structured.completed
model.usage.updated
input.guardrail.checked
input.guardrail.blocked
reasoning.delta
tool.call.args_delta
tool.call.started
tool.call.completed
tool.call.failed
retrieval.query.started
retrieval.query.completed
context.assembly.started
context.assembly.completed
policy.decision
approval.required
approval.resolved
delegation.claimed
delegation.child.created
delegation.completed
delegation.failed
checkpoint.created
context.compaction.started
context.compaction.completed
eval.case.drafted
eval.case.approved
eval.run.started
eval.run.completed
eval.score.recorded
artifact.created
```

Delegation 生命周期事件：

| event type | 稳定 event id | 阶段 payload | 出现条件 |
|---|---|---|---|
| `delegation.claimed` | `delegation:{delegation_id}:claimed` | 公共字段 + `status=claimed` | claim、预算和 event capacity reservation 已原子提交 |
| `delegation.child.created` | `delegation:{delegation_id}:child` | 公共字段 + `child_run_id` + `status`，其中 status 只允许 `queued|running|completed|failed` | child run 已确定创建；local inline 路径允许 child 在 attach 前已终态 |
| `delegation.completed` | `delegation:{delegation_id}:final` | 公共字段 + `status=completed` + 严格符合 5.30 `DelegationSummary` 的完整脱敏 `summary`；不得有顶层 `child_run_id` 或 `error_code` | child terminal 与可信 usage 已完成聚合 |
| `delegation.failed` | `delegation:{delegation_id}:final` | 公共字段 + `status=failed` + `error_code=delegation.execution_failed`；child 已创建时必须附严格符合 5.30 `DelegationSummary` 的完整脱敏 `summary`，child identity 只通过 `summary.children` 表达且不得另加顶层 `child_run_id`；child 创建前不得有 `child_run_id` 或 `summary` | child 受控失败，或 claim 后、child 创建前发生确定性执行失败 |

公共字段固定为 `delegation_id`、`source_agent_id`、`target_agent_id`。四种事件均写 parent `run_id`、parent canonical `trace_id`、source `agent_id`，并固定 `record_scope=run`、`visibility=internal`、`terminal=false`。每次 delegation 最多三条，顺序固定为 claimed -> child.created -> completed|failed，final 互斥；pre-child 确定性失败为 claimed -> failed。edge/policy/tenant/cycle/depth/budget/idempotency/event-capacity 拒绝为零 delegation 业务事件。unknown 结果保持 budget/event reservation 为 reserved/needs_review，阻止 parent terminal 且不发布 final。重试、恢复和 worker reclaim 只能校验或补投上述稳定 event id，不得产生别名或额外生命周期事件。

payload 不得包含 child input、完整 identity/request hash、动态余额、原始 usage、resume token、secret、本地路径或原始异常。RUN-003、CLI 与 RUN-006 默认过滤 `visibility=internal`；只有通过 tenant/run 授权并显式请求 internal visibility 的 reader 才能读取原始 CanonicalEvent。

硬约束：

- 同一 `run_id` 内 `seq` 单调递增。
- 相同 `event_id` 仅在除 sink 分配的 `seq` 与调用方重建重试时不稳定的 `timestamp` 外，其余稳定 envelope 语义完全一致时视为幂等重试并返回原事件。`event_type`、版本、payload/ref/checksum、identity、parent、request/span/raw ref、scope、terminal、visibility、run/tenant/trace 任一不同都必须在 artifact materialize 与 fan-out 前返回脱敏 replay conflict，不得吞掉新 evidence。状态已提交后的 terminal/approval 恢复先读取并校验既有确定性 evidence，只有缺失时才补写，新的 `request_id` 不得重构同一 event-id。
- CanonicalEvent `seq` 的持久化范围固定为 `1..2147483647`。run 创建时先持久化一个 terminal capacity reservation；provider/tool/approval/delegation 等操作必须在外部副作用前，由受信、版本化、封闭的 `operation_kind -> max_prerequisite_events` registry 派生预约数，并通过 `0014` durable evidence outbox 的 run 级锁/CAS 原子预留，业务 agent/HTTP 调用方不得自报容量。容量不变量固定为 `highest_persisted_seq + outstanding_reserved_event_count + terminal_reservation <= 2147483647`；`highest_persisted_seq` 是该 run 已持久化的最大 `seq`，没有 event 时为 `0`，不得用 event row count 替代。预约消费、event 插入与 high-water mark 推进必须在同一 run 锁/事务内原子完成。容量不足时以稳定 `event.sequence_exhausted` 零业务副作用拒绝，不消费 seq。预约只在对应 prerequisite evidence 已持久化或确定不会产生时按实耗结算/释放，未知结果保持预约并阻止 terminal；terminal 消费最后保留的预约且仍是最后一条 event。若历史状态已越过容量不变量、high-water mark 与最大已持久化 `seq` 不一致，或 `seq=2147483647` 不是 terminal，任何新写入以 `event.sequence_state_invalid` 零变更拒绝并要求人工处置。SQLite/local 与 PostgreSQL 必须使用相同规则；稀疏高 seq（例如 `{1, 2147483646}`）必须按最大值拒绝新的 operation reservation并保住 terminal 容量。
- `record_scope` 是只允许 `run|non_run` 的 typed discriminator。当前生产 DTO/OpenAPI 对 `record_scope=run` 要求 `trace_id` 必填、格式合法并与该 run 的 persisted canonical trace 逐值一致；`record_scope=non_run` 允许 `trace_id` 缺失或为 null，持久化与迁移都不得为它生成假的 lineage。RUN-003 和后续 RUN-006 SSE 使用同一字段，不生成第二 trace。
- `terminal=true` 当且仅当 `event_type` 为 `run.completed`、`run.failed` 或 `run.cancelled`；三种 run terminal event 必须显式设置 `terminal=true`、`visibility=public`，其余 36 种类型必须设置 `terminal=false`。EventBus 与 local/PostgreSQL sink 必须在分配 seq、消费容量、物化 artifact 或 fan-out 前拒绝 type/terminal/visibility 任一不一致的 envelope，不能依赖 DTO 默认值。每个 run 只能有一个 terminal event。
- terminal event 是同一 run 的最后一条 CanonicalEvent；持久化后 EventBus/sink 必须拒绝 terminal 和 non-terminal 的任何后续业务事件，不能让已结束的 SSE/JSON 消费者漏掉晚到 evidence。
- terminal 由 durable evidence outbox 协调：usage 结算与 `approval.resolved` 等必需前置 evidence 先按稳定 event id 幂等发布，terminal 最后发布；terminal 一旦可见，恢复不得再补写前置 evidence或重放 provider/tool 副作用。
- `model-usage-evidence` 已通过 `0014` 有序 outbox 原子协调 usage、approval resolution 与 terminal 恢复语义，完整门禁与同 digest 代码 1+2 已通过；change 保持 active 且只到 `ready-to-archive`，不代表已归档、发布或部署。
- `reasoning.delta` 默认不对普通用户可见。
- `model-usage-evidence` SHALL 让 model 与 embedding 精确复用 `model.request.started`、`model.usage.updated`，并以 `ModelUsageEvidence.usage_kind` 区分，不新增等价 embedding event type；单次调用关联固定写在 `payload.correlation.usage_call_id`，类型为非空 string。该值不进入 CanonicalEvent envelope 顶层字段或 `ModelUsageEvidence`；TelemetryFacade 保留相同路径和值。`model.usage.updated` 只结束调用级 usage 生命周期，`CanonicalEvent.terminal=false`，不得关闭 run stream。
- CanonicalEvent envelope 的唯一字节定义为公共 `canonical_event_bytes()`：先取 `CanonicalEvent.to_payload()`，再以 UTF-8、`ensure_ascii=false`、`sort_keys=true`、紧凑 separators `(',', ':')`、`allow_nan=false` 生成 JSON bytes；不计 JSONL 末尾换行、SSE `data:`/frame 分隔符或传输压缩。EventBus、local JSONL、SQLite/PostgreSQL legacy 校验和 SSE byte page 必须调用同一实现，不能各自使用默认 `json.dumps`。正常 envelope 最多 `65536` bytes；大 payload 必须先使用 `payload_ref` 并保留 checksum 或 artifact reference，artifact 化后 envelope 仍超限时，EventBus 以 `event.envelope_too_large` 在持久化和 fan-out 前拒绝。历史或 direct-write 超限 row 读取时返回稳定 `event.envelope_state_invalid`，SSE 转换为一个脱敏 `stream.error` 后关闭，不得返回无 cursor 的空页并忙循环。边界合同必须覆盖恰好等于/超过 `65536` 与 `1048576` bytes、中文/转义字符、不同键插入顺序和 NaN 拒绝。
- `event.sequence_exhausted`、`event.sequence_state_invalid`、`event.envelope_too_large` 与 `event.envelope_state_invalid` 是 EventBus/repository 内部稳定错误码，不新增公开 HTTP status。RUN-006 已握手后遇到非法历史 envelope 时，`stream.error` 的公开 `data.code` 固定为 `stream.event_state_invalid`；握手前若预检即可发现，则使用既有 `500 api.internal_error` envelope，不回显内部行、payload 或 seq 细节。

`run-trace-correlation` 的 local evidence 升级只允许通过离线命令 `agent-harness migrate-local-state --state-dir <dir> (--profile <name> [--profiles-dir <dir>] | --file-only) [--event-path <path>]... [--score-path <path>]...`。必须且只能选择一个模式：profile 模式通过 typed settings 解析关系库配置，credential 只能来自环境或受信 `_FILE`，完整 DSN 不得进入 argv、进程列表、shell history、日志或错误；`--file-only` 只接受显式 non-run records，以及 `payload.telemetry.context.run_id` 为空的 legacy ordinary telemetry，即使其 envelope `run_id` 是合成 trace id 或字面值 `"telemetry"`。显式 run scope、普通 record 的真实非空 `run_id`，或 legacy ordinary telemetry 的非空 nested context `run_id` 都必须立即失败并要求改用 profile 模式。命令冻结 manifest 与显式 legacy paths 后统一预检和迁移；普通 API、CLI run/eval、worker 启动不得自动推进旧 schema。

### 5.10 `AgentDescriptor`

`GET /api/v1/agents` 返回的 public descriptor。它来自 agent registry 的受控 `config.yaml`，不是完整本地配置。

```json
{
  "agent_id": "examples.basic",
  "version": "0.1.0",
  "name": "Basic Example Agent",
  "description": "Offline fake model smoke agent.",
  "input_schema_ref": "agents.examples.basic.schemas.Input",
  "output_schema_ref": "agents.examples.basic.schemas.Output",
  "config_ref": "agents/examples/basic/config.yaml",
  "tool_policy": {
    "allowed_tools": []
  },
  "model_policy": {
    "provider": "fake",
    "default_model": "fake-basic",
    "fallback_models": []
  },
  "budget": {
    "max_tokens_per_run": 8192,
    "max_cost_usd_per_run": null
  },
  "eval_dataset": "eval-cases/drafts/basic.yaml",
  "delegation_targets": []
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `agent_id` | string | Yes | 稳定 agent ID；必须唯一。 |
| `version` | string | Yes | descriptor/config 版本；用于未来兼容与审计，不代表 package version。 |
| `name` | string | Yes | 人类可读名称。 |
| `description` | string | Yes | 简短说明；不得包含 secret。 |
| `input_schema_ref` | string | Yes | 输入 schema 引用，不返回本地绝对路径。 |
| `output_schema_ref` | string | Yes | 输出 schema 引用，不返回本地绝对路径。 |
| `config_ref` | string | Yes | agent config 的仓库相对引用；不得是本机绝对路径。 |
| `tool_policy.allowed_tools` | string[] | Yes | 允许工具摘要；空数组表示无工具权限。 |
| `model_policy.provider` | string | Yes | 模型 provider ID，例如 `fake`。 |
| `model_policy.default_model` | string | Yes | 默认模型 ID。 |
| `model_policy.fallback_models` | string[] | Yes | fallback 模型 ID 列表。 |
| `budget.max_tokens_per_run` | integer | Yes | Parent execution tree 共享 token hard limit；root direct model/embedding、delegation 与 child allocation 统一竞争，不是每个 child 各自获得一份额度。 |
| `budget.max_cost_usd_per_run` | number \| null | Yes | Parent execution tree 共享 cost hard limit；`null` 只表示关闭 shared cost 维度，token 维度仍启用。 |
| `eval_dataset` | string \| null | Yes | eval dataset 引用。 |
| `delegation_targets` | string[] | Yes | 显式允许 delegation 的目标 agent ID。 |

禁止字段：

- 不得返回本地绝对路径、provider secret、API key、callable、provider client、Python module object、SQLAlchemy model 或文件 handle。

### 5.11 `AgentListResponse`

```json
{
  "request_id": "req_123",
  "agents": []
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `request_id` | string | Yes | API 请求关联 ID。 |
| `agents` | `AgentDescriptor[]` | Yes | registry 中已配置 agent 的 public descriptor 列表。空数组表示 registry 可用但没有 agent。 |

### 5.12 `PolicyCheckRequest`

```json
{
  "resource": "tool:shell",
  "action": "shell.execute",
  "context": {
    "run_id": "run_123",
    "trace_id": "trace_123"
  }
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `resource` | string | Yes | 被检查资源，例如 `run:<id>`、`tool:shell`、`policy:default`。 |
| `action` | string | Yes | 稳定动作名，例如 `run.create`、`shell.execute`、`policy.modify`。 |
| `context` | object | No | 调用上下文。只能写摘要、ID、source/trust metadata，不得放完整 secret 或大 payload。 |

### 5.13 `PolicyDecisionResponse`

```json
{
  "request_id": "req_123",
  "decision": "require_approval",
  "reason": "dangerous action requires approval",
  "matched_rules": ["default-dangerous-actions"],
  "audit_ref": "audit_123",
  "approval": {
    "required": true,
    "action": "shell.execute",
    "resource": "tool:shell"
  }
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `request_id` | string | Yes | API 请求关联 ID。 |
| `decision` | `allow` / `deny` / `require_approval` | Yes | policy / guardrail 共用三态决策。 |
| `reason` | string | Yes | 面向调用方的决策原因，必须脱敏。 |
| `matched_rules` | string[] | Yes | 命中的规则摘要；不得暴露内部 Python 对象或 DB handle。 |
| `audit_ref` | string | Yes | audit log 引用；`POST /api/v1/policies/check` 必须返回。 |
| `approval` | object | No | `require_approval` 时的审批摘要，不直接替代 approval record。 |

### 5.14 `ApprovalStatus`

| 值 | 说明 |
|---|---|
| `waiting` | 尚未完成公开 resolution：可能仍在等待人工决定，也可能 approve 已取得私有 lease 但 continuation 尚未得到持久化的确定性结果，或已进入不公开的 `needs_review`；这些私有状态不得进入 DTO/OpenAPI。 |
| `approved` | 人工已允许原动作，且 continuation 已持久化 completed 或确定性 failed result，并让 run 进入对应 terminal；该状态不保证动作执行成功。 |
| `denied` | 已拒绝，动作不得执行，run 按策略 failed 或 fallback。 |
| `cancelled` | 审批被系统或用户取消。 |

### 5.15 `ApprovalRecord`

当前生产 DTO、OpenAPI 与 `0013_run_trace_correlation -> 0013a_run_trace_event_hardening` schema 均要求 `trace_id` 必填且非空；它必须等于关联 run 的 persisted canonical trace。历史 nullable 数据只允许通过显式迁移回填，普通运行入口不得继续写入 null，也不得让调用方覆盖该值。

```json
{
  "approval_id": "approval_123",
  "tenant_id": "default",
  "run_id": "run_123",
  "agent_id": "examples.basic",
  "status": "waiting",
  "action": "shell.execute",
  "resource": "tool:shell",
  "reason": "dangerous action requires approval",
  "trace_id": "trace_123",
  "request_id": "req_123",
  "requested_by": "local-user",
  "resolved_by": null,
  "result": null,
  "created_at": "2026-07-08T00:00:00Z"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `approval_id` | string | Yes | 审批记录稳定 ID。 |
| `tenant_id` | string | Yes | 审批所属租户。 |
| `run_id` | string | Yes | 关联 run。 |
| `agent_id` | string | Yes | 关联 agent。 |
| `status` | `ApprovalStatus` | Yes | 审批状态。 |
| `action` | string | Yes | 被审批动作。 |
| `resource` | string | Yes | 被审批资源。 |
| `reason` | string | Yes | 脱敏后的审批原因。 |
| `trace_id` | string | Yes | run canonical trace；由 approval 从持久化 run context 继承，调用方不得覆盖。历史 nullable 数据由 `run-trace-correlation` 迁移收口。 |
| `request_id` | string | No | 创建审批时的请求关联 ID。 |
| `requested_by` | string | No | 创建审批的 actor user id。 |
| `resolved_by` | string | No | 审批人 user id。 |
| `result` | string | No | `approved` / `denied` 后的结果摘要。 |
| `created_at` | ISO 8601 string | Yes | 创建时间。 |

### 5.16 `ApprovalListResponse`

```json
{
  "request_id": "req_123",
  "approvals": []
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `request_id` | string | Yes | API 请求关联 ID。 |
| `approvals` | `ApprovalRecord[]` | Yes | 当前身份可见的审批列表。空数组表示没有等待或历史审批。 |

### 5.17 `ApprovalResolveRequest`

```json
{
  "decision": "approved",
  "comment": "reviewed in CLI"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `decision` | `approved` / `denied` | Yes | 本次 resolve 结果。 |
| `comment` | string | No | 审批备注，必须脱敏后写入 audit。 |

### 5.18 `ApprovalResolveResponse`

```json
{
  "request_id": "req_123",
  "approval": {
    "approval_id": "approval_123",
    "tenant_id": "default",
    "run_id": "run_123",
    "agent_id": "examples.basic",
    "status": "approved",
    "action": "shell.execute",
    "resource": "tool:shell",
    "reason": "dangerous action requires approval",
    "request_id": "req_456",
    "created_at": "2026-07-08T00:00:00Z"
  },
  "run": {
    "request_id": "req_123",
    "run_id": "run_123",
    "status": "completed",
    "terminal_event": "run.completed"
  }
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `request_id` | string | Yes | API 请求关联 ID。 |
| `approval` | `ApprovalRecord` | Yes | resolve 后的审批记录。 |
| `run` | `RunCreateResponse` | No | approve/deny 推进 run 后的 public runtime 摘要。 |

### 5.19 `ToolCallRequest`

CLI/runtime/module seam 使用的工具调用 DTO；当前不暴露为 HTTP request body。

```json
{
  "agent_id": "dev-assistant",
  "run_id": "run_123",
  "tool_name": "file.read_file",
  "arguments": {
    "path": "README.md"
  },
  "request_id": "req_123",
  "trace_id": "trace_123"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `agent_id` | string | Yes | 发起工具调用的 agent；用于 allowlist、policy、audit 和 trace 关联。 |
| `run_id` | string | No | 关联 run；CLI 单独验证时可为空，但生产 runtime 调用必须提供。 |
| `tool_name` | string | Yes | 稳定工具名，例如 `file.read_file`、`shell.execute`、`mcp.<server>.<tool>`。 |
| `arguments` | object | Yes | 工具输入；必须先过工具 descriptor 的 input schema validation。 |
| `request_id` | string | No | 调用方 request id；没有时由 CLI/runtime seam 生成。 |
| `trace_id` | string | No | 调用方 trace id；用于 tool event、audit 和 tool invocation 记录。 |

### 5.20 `ToolCallResult`

工具调用稳定结果 DTO；当前不作为 HTTP response body 直接暴露。

```json
{
  "tool_name": "shell.execute",
  "status": "completed",
  "result": {
    "exit_code": 0,
    "stdout": "short output",
    "stdout_ref": "artifact://stdout-large",
    "stderr": "",
    "stderr_ref": null
  },
  "source_ref": "tool://shell.execute/run_123/inv_123",
  "trust_level": "untrusted",
  "artifact_ref": "artifact://...",
  "truncation": {
    "truncated": true,
    "original_bytes": 20000,
    "retained_bytes": 4096
  },
  "policy": {
    "decision": "allow"
  },
  "error": null,
  "request_id": "req_123",
  "trace_id": "trace_123",
  "invocation_id": "inv_123"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `tool_name` | string | Yes | 被执行的工具名。 |
| `status` | string | Yes | `completed`、`failed`、`denied`、`requires_approval`、`disabled` 或 `timeout`。 |
| `result` | object | No | 小结果摘要；不得包含完整大 payload、secret 或 provider 原始对象。ShellTool 的 `stdout_ref` / `stderr_ref` 位于此对象内，值必须是 artifact ref。 |
| `source_ref` | string | Yes | 可追踪来源引用，进入 ContextAssembler 前必须保留。 |
| `trust_level` | string | Yes | tool/MCP 输出默认 `untrusted`；本地元数据可为 `system` 或 `trusted`，但内容输出不得默认 trusted。 |
| `artifact_ref` | string | No | 整体工具结果、文件内容或 MCP payload 的主 artifact 引用；ShellTool 分流 stdout/stderr 时使用 `result.stdout_ref` / `result.stderr_ref`。 |
| `truncation` | object | Yes | 至少包含 `truncated`；截断时包含原始/保留大小。 |
| `policy` | object | Yes | policy decision 摘要；包含 `decision`、`reason` 和可选 `audit_ref`。 |
| `error` | object / null | No | 失败或拒绝时的稳定错误摘要，形状复用 `ErrorDetail` 字段；成功时为空。 |
| `request_id` | string | No | 请求关联 ID。 |
| `trace_id` | string | No | trace 关联 ID。 |
| `invocation_id` | string | Yes | `tool_invocations` 持久化记录 ID 或等价稳定引用。 |

### 5.21 Tool execution error codes

工具 seam 必须使用稳定错误码，CLI、runtime 和未来 API route 都按这些 code 分支，不解析人类文案。

| code | status | 触发场景 |
|---|---|---|
| `tool.not_found` | `failed` | `ToolRegistry` 找不到请求的 `tool_name`。 |
| `tool.schema_validation_failed` | `failed` | `arguments` 不符合工具 input schema，且目标工具没有执行。 |
| `tool.policy_denied` | `denied` | `PolicyEngine` 返回 `deny`，目标工具没有执行。 |
| `tool.approval_required` | `requires_approval` | `PolicyEngine` 返回 `require_approval`，目标工具没有执行，调用方必须进入 approval seam。 |
| `tool.disabled` | `disabled` | ShellTool 或某个工具未显式启用。 |
| `tool.timeout` | `timeout` | Shell/MCP/工具执行超过 timeout 并被终止或取消。 |
| `tool.workspace_denied` | `denied` | workspace root 外路径或 `.agentignore` 命中导致 FileTool 拒绝。 |
| `tool.allowlist_denied` | `denied` | shell command、MCP server 或 MCP tool 未在 allowlist 内。 |
| `tool.execution_failed` | `failed` | 工具实现执行失败；错误摘要已脱敏，不包含 provider 原始异常或 secret。 |

### 5.22 `HealthResponse`

`GET /api/v1/health` 的公开只读响应。它只表达应用已启动及当前 profile 配置的 capability 摘要，不回显连接字符串，也不替代 `make smoke-service` 的真实依赖探测。

```json
{
  "request_id": "req_123",
  "status": "ok",
  "profile": "local",
  "storage": {"kind": "sqlite", "status": "configured"},
  "queue": {"kind": "in-memory", "status": "configured"},
  "observability": {"kind": "local-jsonl", "status": "configured"}
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `request_id` | string | Yes | 请求关联 ID；调用方未传 `X-Request-Id` 时由服务端生成。 |
| `status` | `ok` / `degraded` | Yes | app liveness 与配置装配摘要；不代表 PostgreSQL/Redis/provider 已完成网络探测。 |
| `profile` | string | Yes | 当前类型化 profile 名称，不得包含绝对路径。 |
| `storage` | object | Yes | 仅包含 `kind` 与 `status`，不得包含 DSN、密码或本机路径。 |
| `queue` | object | Yes | 仅包含 `kind` 与 `status`，不得包含 Redis URL、密码或 token。 |
| `observability` | object | Yes | 仅包含 `kind` 与 `status`，不得包含 endpoint credential、token env 的值或 provider 原始对象。 |

### 5.23 `HarnessVersionManifest`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `version_id` | string | Yes | 规范化 inputs manifest 的稳定 SHA-256 标识；服务端必须校验与内容一致。 |
| `inputs` | object | Yes | 必须覆盖 prompt/instruction、tool description、agent/retrieval/policy config、model profile/adapter settings；每项只保存 checksum、脱敏 diff summary 和 evidence ref。 |

### 5.24 `EvalExperimentCreateRequest`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `agent_id` | string | Yes | experiment 目标 Agent。 |
| `dataset` | string | Yes | approved dataset 名称。 |
| `tags` | string[] | Yes | 至少一个 approved case metadata 中存在的 behavior tag。 |
| `split_strategy` | `deterministic_multilabel_v1` | Yes | 初始版本唯一允许的 split 策略。 |
| `baseline_harness_version` | `HarnessVersionManifest` | Yes | baseline 行为输入清单。 |
| `candidate_harness_version` | `HarnessVersionManifest` | No | 省略时只创建不可变 baseline snapshot。 |
| `optimization_ratio` | number | No | 默认 0.8；与 holdout ratio 相加必须为 1。 |
| `holdout_ratio` | number | No | 默认 0.2；必须保持非空 holdout。 |
| `regression_policy` | object | No | 固定/关键 case refs、metadata flag、critical tags 和 holdout regression 阈值。 |
| `metadata` | object | No | 已脱敏、provider-neutral 的维护者 metadata。 |

### 5.25 `EvalExperimentResponse`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `request_id` | string | Yes | 当前 API 请求关联 ID。 |
| `experiment_id` | string | Yes | 持久化 experiment ID。 |
| `status` | enum | Yes | `running`、`baseline_completed`、`completed`、`failed`、`needs_review`、`baseline_completed_with_degradation`、`completed_with_degradation`。正常新建在原子 claim 后对外为 `running` 或终态；旧 0009 遗留的 `created` 结果不确定，0011 数据升级或重放时必须先转 `needs_review`，不得作为公共状态继续执行。 |
| `agent_id` | string | Yes | experiment 目标 Agent。 |
| `dataset` | string | Yes | approved dataset。 |
| `tags` | string[] | Yes | 本次 experiment 请求标签。 |
| `optimization_case_count` | integer | Yes | optimization subset case 数。 |
| `holdout_case_count` | integer | Yes | holdout subset case 数。 |
| `regression_case_count` | integer | Yes | regression subset case 数。 |
| `baseline_harness_version` | string | Yes | 已校验 baseline version ID。 |
| `candidate_harness_version` | string | No | 已校验 candidate version ID。 |
| `baseline_eval_run_ref` | string | Yes | baseline eval run/evidence ref。 |
| `candidate_eval_run_ref` | string | No | candidate eval run/evidence ref。 |
| `local_evidence_refs` | string[] | Yes | 本地真相源 refs。 |
| `provider_statuses` | object[] | Yes | 已脱敏 provider success/degraded 摘要。 |

### 5.26 `EvalExperimentComparisonResponse`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `request_id` | string | Yes | 当前 API 请求关联 ID。 |
| `experiment_id` | string | Yes | comparison 所属 experiment。 |
| `candidate_harness_version` | string | Yes | 本次 comparison 实际评估的 candidate version。 |
| `per_tag` | object[] | Yes | 每个标签的 baseline score、candidate score 和 delta。 |
| `holdout_delta` | number | Yes | holdout aggregate delta。 |
| `regressions` | object[] | Yes | 退化项与 evidence refs。 |
| `new_failures` | object[] | Yes | candidate 新失败项与 evidence refs。 |
| `fixed_failures` | object[] | Yes | candidate 修复项与 evidence refs。 |
| `acceptance_recommendation` | `accept` / `reject` / `needs_review` | Yes | 仅供人工 review，不产生 acceptance side effect。 |
| `recommendation_reason_codes` | `target_tag_improved` / `named_failure_fixed` / `no_target_improvement` / `holdout_within_threshold` / `holdout_regression_exceeded` / `critical_regression_passed` / `critical_regression_failed` / `new_failures_present` / `local_evidence_incomplete` / `comparison_incomplete` 的非空数组 | Yes | 稳定、可机读的推荐依据；OpenAPI 必须声明 `minItems: 1` 和该封闭枚举。 |
| `local_evidence_refs` | string[] | Yes | comparison 本地真相源 refs。 |
| `provider_statuses` | object[] | Yes | 已脱敏 provider success/degraded 摘要。 |

### 5.27 `EvalExperimentAcceptanceRequest`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `decision` | `accepted` / `rejected` | Yes | 人工 review 决策。 |
| `reason` | string | Yes | 非空维护者理由。 |
| `accepted_harness_version` | string | Conditional | accepted 时必填且必须等于 comparison candidate；rejected 时必须为空。 |
| `followup_issue_ref` | string | No | 后续整改或调查引用。 |

### 5.28 `EvalExperimentAcceptanceResponse`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `request_id` | string | Yes | 当前 API 请求关联 ID。 |
| `experiment_id` | string | Yes | 人工 decision 所属 experiment。 |
| `decision_id` | string | Yes | 每个 experiment 唯一且不可变的 review decision ID。 |
| `decision` | `accepted` / `rejected` | Yes | 已持久化决策。 |
| `reviewer_id` | string | Yes | 来自认证 identity，不接受 body 覆盖。 |
| `accepted_harness_version` | string | No | accepted production binding 的 candidate version；rejected 时为空。 |
| `production_binding` | boolean | Yes | 只有 accepted 且门禁/policy 通过时为 true。 |
| `policy_decision` | object | Yes | 已脱敏 policy 结果与 matched rule 摘要。 |
| `audit_ref` | string | Yes | 唯一 decision audit ref。 |
| `evidence_refs` | string[] | Yes | comparison/holdout/regression evidence refs。 |
| `followup_issue_ref` | string | No | rejected 或后续整改使用的安全逻辑引用。 |

### 5.29 `ModelUsageEvidence`

model 与 embedding adapter 共用的 provider-neutral 调用证据。业务 agent 不得自行拼接 provider 原始 usage 或异常。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `usage_kind` | `model` / `embedding` | Yes | 调用类型。 |
| `tenant_id` | string | Yes | 持久化 evidence 的直接租户归属，不得只从 run 推导。 |
| `provider` | string | Yes | provider ID，不含 client 或 endpoint credential。 |
| `model` | string | Yes | 实际模型 ID。 |
| `input_tokens` | integer \| null | Yes | provider 可用时记录非 bool、`>=0` 的整数；不可用时为 null。 |
| `output_tokens` | integer \| null | Yes | provider 可用时记录非 bool、`>=0` 的整数；embedding 或不可用时可为 null。 |
| `cost_usd` | number \| null | Yes | 可用时必须是非 bool、有限且 `>=0` 的 USD 数值；不可用时为 null，不能伪造为 0。 |
| `cost_status` | `reported` / `estimated` / `unavailable` | Yes | `reported|estimated` 要求 `cost_usd` 非 null；`unavailable` 要求 `cost_usd=null`。 |
| `latency_ms` | integer | Yes | 本次 adapter 调用墙钟时延，必须是非 bool、`>=0` 的整数。 |
| `decision` | object | Yes | route、fallback、cache/provider-side-effect、budget/policy decision 的 provider-neutral 摘要；cache hit 必须有 `cache_status=hit`、`provider_called=false`；`cost_status=estimated` 时必须包含安全的 `price_source_ref` 与 `price_source_version`，不得内联完整价目或 provider raw payload。 |
| `run_id` | string | Yes | 所属 run。 |
| `agent_id` | string | Yes | 所属 agent。 |
| `request_id` | string | No | 请求关联 ID。 |
| `trace_id` | string | Yes | 可与 CanonicalEvent、OTel 和 parent aggregation 对账的 trace ID。 |

所有 usage 数值在持久化、EventBus 发布和 delegation 聚合前统一校验；bool、负数、NaN、正负 Infinity 以及 `cost_usd/cost_status` 不一致必须结构化拒绝，不能通过求和反向冲减预算。真实零 token/cost 合法，但必须与 null/`unavailable` 分开。embedding cache hit 仍是一轮调用级 usage 生命周期：发布 started/final evidence，`latency_ms` 记录本次 cache lookup 墙钟，token/cost 为 null 且 `cost_status=unavailable`，`decision.cache_status=hit`、`decision.provider_called=false`；不得复用 cache row 的首次 `provider_latency_ms`，也不得再次调用 provider。

### 5.30 `DelegationSummary`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `parent_run_id` | string | Yes | 发起 delegation 的 parent run。 |
| `children` | object[] | Yes | 成员以 durable parent-child relation 为真相源，不以 terminal aggregate row 是否存在为准。每项只含 child `run_id`、`agent_id`、`status`、usage evidence refs 和 trace refs；`status` 直接来自持久化 child `RunStatus`，允许 `created|running|waiting|completed|failed|cancelled`。已持久化但未结算的 child 也必须出现。 |
| `input_tokens` | integer \| null | Yes | 已知 child model evidence 的输入 token 合计；混合已知/未知时保留已知和，全部未知时为 null。任一 child 为 null 时不得把未知值当 0，且 `budget_status` 必须为 `incomplete`。 |
| `output_tokens` | integer \| null | Yes | 已知 child model evidence 的输出 token 合计；混合已知/未知时保留已知和，全部未知时为 null。任一 child 为 null 时不得把未知值当 0，且 `budget_status` 必须为 `incomplete`。 |
| `latency_ms` | integer \| null | Yes | 仅当所有 child latency 都可用时求和；任一 child latency 未知时为 null 且 `budget_status` 必须为 `incomplete`，不得把未知值当 0。 |
| `cost_usd` | number \| null | Yes | 所有可用 child cost 合计；存在 unavailable cost 时必须为 null，并在 `budget_status` 解释。 |
| `budget_status` | `within_budget` / `exceeded` / `incomplete` | Yes | parent 对 child usage/cost 的预算影响。 |
| `trace_refs` | string[] | Yes | 去重后的 child trace refs；不得包含 provider raw payload。 |

仅活动 child，或已终态但尚未写入可信 aggregation 的 child，其 token/cost/latency 均视为未结算 unknown：对应数值为 null，`budget_status=incomplete`。已结算与未结算 child 并存时，`children` 必须包含全部 durable relation；token 只累计已知值，cost/latency 继续按全体完整性规则返回 null，整体 `budget_status` 保持 incomplete。不得因为 aggregate row 暂缺而把已有 child relation 解释为“没有 child”。

### 5.31 `RunDetailResponse`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `request_id` | string | Yes | 当前 API 请求关联 ID。 |
| `run_id` | string | Yes | run 稳定 ID。 |
| `agent_id` | string | Yes | 当前 agent。 |
| `status` | `RunStatus` | Yes | 当前状态。 |
| `terminal_event` | string \| null | Yes | terminal event type，非终态为 null。 |
| `parent_run_id` | string \| null | Yes | child run 指向 parent；根 run 为 null。 |
| `delegation_summary` | `DelegationSummary` \| null | Yes | parent run 的 relation-first durable 投影。只要存在带 `child_run_id` 的持久化 parent-child relation 就必须非 null，并覆盖全部已结算/未结算 child；当且仅当确实不存在这类 durable relation 时为 null。 |

## 6. Run API

### RUN-001 创建 agent-scoped run

| 字段 | 内容 |
|---|---|
| Contract ID | `RUN-001` |
| 状态 | 已实现。 |
| 入口 / 调用方 | OpenAPI 调用方、service-app、未来 Access/API gateway；CLI 等价入口为 `agent-harness run <agent_id>` |
| 用途 | 为指定 agent 创建一次 run，并通过 runtime seam 写入 run lifecycle 和 events。 |
| 方法 | `POST` |
| 路径 | `/api/v1/agents/{agent_id}/runs` |
| 认证 | 已接入 `IdentityContext` dependency；local/dev 未配置 verifier 时注入默认身份，service/API key profile 要求 `Authorization: Bearer <token>` 或等价 API key；创建前必须通过 `run.create` policy check。 |
| 请求头 | `Content-Type: application/json`；可选 `Accept: application/json`、`X-Request-Id`、`X-Trace-Id`；认证 profile 启用 verifier 时必填 `Authorization: Bearer <token>` 或等价 API key。缺失 trace 由服务端在业务副作用前生成。 |
| Path 参数 | `agent_id: string`，稳定 agent ID。Agent Registry 能力落地后必须由 `AgentRegistry` 校验存在性和重复性。 |
| URL 参数 | none |
| 请求体 | `AgentRunCreateRequest` |
| 幂等性 | body 含 `idempotency_key` 时，同一 tenant/agent/session 下重复提交返回同一 run；缺失时非幂等。 |
| 副作用 | local profile 写 run/checkpoint/events并 inline 执行；service profile 先写 `created+enqueue_pending` 私有状态，Redis接受并完成 queued/message/`run.queued` 对账后由独立 worker执行，API进程不得调用 executor。 |
| 成功响应码 | local profile `200`；service profile queued成功 `202`。 |
| 响应头 | 当前只保证 `Content-Type: application/json`；不保证 `X-Request-Id` response header。 |
| 响应体 | `RunCreateResponse`。 |
| 错误响应码 | `400 api.http_error`、`401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied` / `guardrail.denied`、`404 registry.agent_not_found` / `api.not_found`、`409 run.invalid_transition` / `trace.conflict` / `trace.idempotency_conflict`、`422 validation_error` / `registry.invalid_config`、`503 run.enqueue_unavailable`、`500 api.internal_error`。 |
| 状态语义 | service 202 返回 `created`，且只有 Redis接受、repository保存 queued/message ref并发布唯一 `run.queued` 后才算成功；enqueue任一步失败返回503并保留可补投私有状态。同客户端 key重试复用原 run/operation/首次 request id；无客户端 key的新请求仍非幂等，但原 pending run由worker startup/pickup recovery补投。`completed/failed/cancelled` 表示 terminal；`waiting` 表示需要 approval 或 resume。私有 queue字段不进入响应/OpenAPI。 |
| 安全规则 | API route 不得直接操作 ORM session、DBOS API 或 provider SDK；input 进入 runtime 前必须经过 `run.create` policy check 和 guardrail/trust 标注；无效 token 或缺少 `run.create` 权限不得创建 run。 |
| 验证要求 | legacy route/OpenAPI 由 `tests/contracts/test_runtime_checkpoint_runs_contracts.py` 锁定；service enqueue、身份 fencing 与 worker recovery 分别由 `test_split_runtime_execution_contracts.py`、`test_split_runtime_worker_recovery_contracts.py` 锁定；`make smoke-service` 必须在 workspace 外以真实 API key、PostgreSQL、Redis 和独立 worker证明 HTTP-to-worker、hard crash/reclaim、唯一 terminal 与重复提交同 run。认证/策略/HITL tests 必须覆盖无效 token 零 run/queue/audit、guardrail deny 和 require_approval。 |

### CLI-RUN-001 创建 agent-scoped run

| 字段 | 内容 |
|---|---|
| Contract ID | `CLI-RUN-001` |
| 状态 | 已实现可选 `--trace-id`、canonical trace 生成与稳定冲突错误。 |
| 命令 | `agent-harness run <agent_id> [--trace-id <value>]`，其余 profile/storage/events/agents/idempotency/prompt 选项保持既有语义。 |
| Trace 输入 | `--trace-id` 缺失时在业务副作用前生成 lowercase RFC 4122 UUID；提供时必须原样匹配 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`，不 trim、不折叠大小写，并与 RUN-001 使用同一 normalizer。 |
| 幂等与冲突 | 同一 idempotency key 缺失 trace 或提供首次 canonical trace 时复用首次 run；提供不同 trace 返回 `trace.idempotency_conflict`。已绑定其他 root run 返回 `trace.conflict`；格式非法返回 `validation_error`。 |
| 输出与退出 | 成功保持既有 run/status/terminal stdout；trace validation/conflict 只向 stderr 写稳定错误 code 和安全摘要并非零退出，不回显其他 tenant/root 绑定信息。 |
| 副作用 | 非法、全局冲突或 idempotency trace 冲突必须在 run、event、queue、approval、tool/model/provider 副作用前失败。 |
| 验证要求 | CLI runner contracts 覆盖缺失生成、合法值保留、空白/超长/非法字符、全局冲突、同 key 相同/不同 trace、stderr/exit 与逐表/queue/provider side-effect count，并与 RUN-001 对同一 normalizer 做双向断言。 |

### RUN-002 读取 run detail

| 字段 | 内容 |
|---|---|
| Contract ID | `RUN-002` |
| 状态 | 已切换为 `RunDetailResponse`，并从 durable delegation aggregation 读取 parent 汇总。 |
| 入口 / 调用方 | OpenAPI 调用方、service-app、未来 Access/API gateway。 |
| 用途 | 按 `run_id` 读取 run 当前状态，不暴露 ORM model 或内部 handle。 |
| 方法 | `GET` |
| 路径 | `/api/v1/runs/{run_id}` |
| 认证 | 已接入 `IdentityContext` dependency；按 tenant/identity 可见性检查，local/dev 未配置 verifier 时使用默认身份。 |
| 请求头 | 可选 `Accept: application/json`、`X-Request-Id`；认证 profile 启用 verifier 时必填 `Authorization`。 |
| Path 参数 | `run_id: string` |
| URL 参数 | none |
| 请求体 | none |
| 幂等性 | 幂等读取。 |
| 副作用 | none。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `RunDetailResponse`；route、OpenAPI 和 drift test 已在 `agent-delegation-execution` 中原子切换。 |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`500 api.internal_error`。 |
| 状态语义 | 调用方根据 `status` 和 `terminal_event` 判断继续轮询、读取 events、resume、cancel 或展示终态。 |
| 安全规则 | 非当前 tenant 或不可见 run 必须返回 `404` 或 `403`，不能泄漏其他 tenant 的 run 是否存在。 |
| 验证要求 | OpenAPI schema 必须包含 `request_id`；404 必须走 `ApiErrorEnvelope`。 |

### RUN-003 读取 run events

| 字段 | 内容 |
|---|---|
| Contract ID | `RUN-003` |
| 状态 | 已实现为 JSON event read seam；P0 SSE transport 由待实现 RUN-006 承担，P1 可选 WS；都不是当前 route。 |
| 入口 / 调用方 | OpenAPI 调用方、service-app、未来 SSE adapter、debug 工具、worker smoke。 |
| 用途 | 按 `seq` 读取 `CanonicalEvent`，供断线恢复、debug、SSE/API resume 共用。 |
| 方法 | `GET` |
| 路径 | `/api/v1/runs/{run_id}/events` |
| 认证 | 已接入 `IdentityContext` dependency；按 tenant/identity/event visibility 检查，`include_internal=true` 需要 policy 权限。 |
| 请求头 | 可选 `Accept: application/json`、`X-Request-Id`；认证 profile 启用 verifier 时必填 `Authorization`。 |
| Path 参数 | `run_id: string` |
| URL 参数 | `after_seq: integer >= 0`，默认 `0`；`include_internal: boolean`，默认 `false`。 |
| 请求体 | none |
| 幂等性 | 幂等读取；同一 `after_seq` 可重复读取同一事件窗口。 |
| 副作用 | none。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。RUN-006 必须使用 `text/event-stream`，不能复用本 JSON route 伪装成 SSE。 |
| 响应体 | `RunEventsResponse` |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`422 validation_error`、`500 api.internal_error`。 |
| 状态语义 | 空数组表示当前没有新事件，不等于 run 已结束；terminal event 的 `terminal=true` 才是最终结算信号。 |
| 安全规则 | `include_internal=false` 时必须只返回 public event；`include_internal=true` 需要权限。 |
| 验证要求 | contract tests 必须覆盖 `reasoning.delta` 默认隐藏、`include_internal=true` 可见、OpenAPI path 存在和 event seam 可读。 |

### RUN-004 取消 run

| 字段 | 内容 |
|---|---|
| Contract ID | `RUN-004` |
| 状态 | 已实现 |
| 入口 / 调用方 | OpenAPI 调用方、service-app、未来 Access/API gateway。 |
| 用途 | 取消尚未 terminal 的 run。 |
| 方法 | `POST` |
| 路径 | `/api/v1/runs/{run_id}/cancel` |
| 认证 | 已接入 `IdentityContext` dependency；认证 profile 启用 verifier 时需要有效 Bearer/API key。 |
| 请求头 | 可选 `Accept: application/json`、`X-Request-Id`；认证 profile 启用 verifier 时必填 `Authorization`。 |
| Path 参数 | `run_id: string` |
| URL 参数 | none |
| 请求体 | none |
| 幂等性 | 对非 terminal run 非幂等；对已 terminal run 必须返回 `409 run.invalid_transition`，不得改写终态。 |
| 副作用 | 更新 run status，写 terminal cancel event 和 audit/event evidence。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `RunCreateResponse` |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`409 run.invalid_transition`、`500 api.internal_error`。 |
| 状态语义 | 成功后 `status=cancelled`，`terminal_event=run.cancelled`。 |
| 安全规则 | 取消动作不得绕过 policy/audit；后续 worker 分进程时必须通过 runtime seam 协调，不直接杀进程作为唯一状态来源。 |
| 验证要求 | runtime transition tests 必须证明 terminal run 不能再取消。 |

### RUN-005 resume run

| 字段 | 内容 |
|---|---|
| Contract ID | `RUN-005` |
| 状态 | 已实现。普通 checkpoint 可走公开 resume；approval-gated checkpoint 的公开请求在消费 token 或调用 handler 前稳定返回 `409 run.invalid_transition`，真实 continuation 只由 `APR-002` 的私有 lease → `ApprovalGrant` → runtime 内部 resume 执行链推进。 |
| 入口 / 调用方 | OpenAPI 调用方、普通 checkpoint 恢复、future worker/API gateway。HITL approval flow 不得把本 endpoint 当作 approve 后的公开执行入口。 |
| 用途 | 使用 resume token 恢复非 approval-gated 的 checkpointed run。approval-gated checkpoint 只能由 `APR-002` 先取得私有 resolution lease、生成绑定 `ApprovalGrant`，再通过 runtime 内部 resume seam 推进。 |
| 方法 | `POST` |
| 路径 | `/api/v1/runs/{run_id}/resume` |
| 认证 | 已接入 `IdentityContext` dependency；认证 profile 启用 verifier 时需要有效 Bearer/API key，resume token 还必须属于 path run。 |
| 请求头 | `Content-Type: application/json`；可选 `Accept: application/json`、`X-Request-Id`；认证 profile 启用 verifier 时必填 `Authorization`。 |
| Path 参数 | `run_id: string` |
| URL 参数 | none |
| 请求体 | `RunResumeRequest` |
| 幂等性 | 非幂等；token 已消费或 run 已 terminal 时不得推进状态。approval-gated checkpoint 的公开请求必须在消费 token、推进 run 或调用 handler 前稳定拒绝。 |
| 副作用 | 对普通 checkpoint 解析 resume token、推进 run state并写后续 events，可能触发已获授权的 worker/model 后续动作；对 approval-gated checkpoint 不得产生 run、event、tool handler、resolution 或 audit outcome 副作用，冲突审计除外。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `RunCreateResponse` |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`409 run.invalid_transition`、`422 validation_error`、`500 api.internal_error`。公开请求命中 approval-gated checkpoint 时固定返回 `409 run.invalid_transition`，提示调用方改用 approval resolve 入口。 |
| 状态语义 | 普通 checkpoint 成功后返回新的 run status；如果完成则返回 terminal event。approval-gated checkpoint 的公开请求始终不改变 run/approval 状态。 |
| 安全规则 | `resume_token` 必须属于 path 中的 `run_id` 并匹配 tenant/identity；错误 URL 不得推进其他 run。原始 token 即使同时匹配 approval context，也不足以执行 approval-gated 动作；该动作只能由 `APR-002` 取得私有 lease 后生成的 `ApprovalGrant` 经内部 resume seam 推进。 |
| 验证要求 | contract tests 必须覆盖 token/run_id mismatch 先失败且不推进任一 run，并覆盖 approval-gated checkpoint 直接调用公开 `RUN-005` 返回 `409 run.invalid_transition`、tool handler 执行计数为零、token 未消费且 run/approval 状态不变。 |

### RUN-006 流式读取 run events

| 字段 | 内容 |
|---|---|
| Contract ID | `RUN-006` |
| 状态 | 规划中（P0）；不得用 RUN-003 JSON response 冒充 SSE。 |
| 入口 / 调用方 | OpenAPI 调用方、service-app、未来 Access gateway。 |
| 用途 | 将可见 `CanonicalEvent` 映射为 SSE frame，并按 `Last-Event-ID` 恢复未读事件。 |
| 方法 | `GET` |
| 路径 | `/api/v1/runs/{run_id}/events/stream` |
| 认证 | 复用 RUN-003 的 IdentityContext、tenant/run 可见性和 event visibility policy。 |
| 请求头 | `Accept: text/event-stream`；可选 `Last-Event-ID: <CanonicalEvent.seq>`，十进制整数范围固定为 `0..2147483647`；以及 `X-Request-Id`、认证 profile 所需 `Authorization`。OpenAPI header schema 必须声明 `minimum=0`、`maximum=2147483647`。 |
| Path 参数 | `run_id: string`。 |
| URL 参数 | `include_internal: boolean=false`；true 时需要同 RUN-003 的额外权限。不得再接受 `after_seq`，避免两个续读真相源。 |
| 请求体 | none |
| 幂等性 | 幂等订阅；相同 `Last-Event-ID` 从同一后继 seq 开始。 |
| 副作用 | none；读取不得创建 run/event/audit outcome，连接审计只能写脱敏 read evidence。 |
| 成功响应码 | `200` |
| 响应头 | `Content-Type: text/event-stream`、`Cache-Control: no-cache`；代理缓冲必须关闭或在部署文档明确。 |
| 响应体 | SSE frame：`id=<seq>`、`event=<event_type>`、`data=<CanonicalEvent JSON>`；frame data 保留 event_version、terminal、visibility、request_id/trace_id。 |
| 错误响应码 | 握手前使用 `401/403/404/422/500 ApiErrorEnvelope`；握手后错误转换为脱敏 `event: stream.error` frame 后关闭连接。 |
| 状态语义 | 只发送 `seq > Last-Event-ID` 的可见事件；run terminal event 发送后关闭。若合法 cursor 已消费当前 run 的 terminal marker，握手后立即 EOF，不重放 terminal、不发送 heartbeat。只有 run 尚未 terminal 且暂时没有新事件时才可发送 comment heartbeat；heartbeat 不占 CanonicalEvent seq。P0 transport 不增加 CanonicalEvent 清理、TTL 或 retention job：run 存续期间其 event evidence 不删除，曾合法的非零 cursor 不会因本 transport 的后台行为变成过期 cursor；未来 retention 必须另建行为 change 并定义 expired-cursor 契约。 |
| 背压 | EventSink 按 exclusive `after_seq` 提供受限分页；每条 event 必须先通过 `canonical_event_bytes() <= 65536` 的单条合法性校验，再计入默认每页最多 `100` 个 event、合法 envelope 合计最多 `1048576` bytes 的 page budget。generator 同时最多持有一个 page，逐 frame 等待 ASGI send 完成后才继续，不得预取下一页。达到 event 或 page bytes 任一上限即以最后已发送 seq 续读；客户端断连或 send cancellation 立即停止读取。 |
| 安全规则 | 默认隐藏 reasoning/internal event；header 必须解析为 `0..2147483647` 内的十进制整数，该上限与 PostgreSQL `canonical_events.seq` 的 `Integer` schema 一致。`0` 是无需命中既有 seq 的合法初始 cursor，其他值必须命中当前身份与 `include_internal` 权限下可见的既有 event seq。隐藏空洞、其他 run seq、不存在 seq 与整数越界返回同一 422，不得形成 internal event 存在性 oracle；不得把 provider raw event、resume token、secret 或内部异常写入 frame。 |
| 验证要求 | 局部 OpenAPI drift + transport contract 覆盖 content type、frame 映射、默认可见性、缺失/`0`/可见既有/隐藏/非法 Last-Event-ID、统一 422、握手后错误、terminal 关闭与已有事件首 frame <1s；local/PostgreSQL 还必须覆盖 run/operation capacity reservation、稀疏高 seq/high-water mark、容量不足副作用前拒绝、未知结果保留预约、非法历史容量状态、canonical serializer 的 Unicode/键顺序/NaN/精确边界、单条 envelope `65536/65537` 与 page 累计 `1048576/1048577` 四点、`100` event 上限、慢客户端无预取、断连取消，以及 P0 无 retention 清理。 |

### CLI-EVT-001 流式读取 run events

| 字段 | 内容 |
|---|---|
| Contract ID | `CLI-EVT-001` |
| 状态 | 规划中（P0）；满足 REQ-014 的 CLI stream adapter，不得以 run 完成后的三行摘要冒充流式输出。 |
| 命令 | `agent-harness events stream <run_id> [--after-seq <0..2147483647>] [--include-internal]`，并复用既有 profile/storage/events-path/identity 配置选项。 |
| 用途 | 通过同一授权 EventSink reader 在终端逐条消费 `CanonicalEvent`；不经 HTTP/SSE framing，也不建立第二套 event store。 |
| Cursor | `--after-seq` 是 CLI 专属 exclusive cursor，默认 `0`；非零值必须命中当前身份与 `--include-internal` 权限下可见的既有 event。它不进入 RUN-006 query，也不改变 HTTP 唯一 `Last-Event-ID` 契约。 |
| stdout | 每个可见 event 恰好一行 UTF-8 NDJSON，内容必须是 `canonical_event_bytes(event).decode('utf-8')`；不得混入状态提示、heartbeat、日志或 provider 原始 payload。 |
| stderr / exit | 输入、授权、cursor 或 legacy envelope 错误使用稳定脱敏 code 写 stderr 并非零退出；已输出部分 event 后的读取错误也不得向 stdout 伪造 CanonicalEvent。Ctrl-C 使用中断退出且停止 reader。 |
| 状态语义 | 严格按 seq 递增输出 `seq > after_seq` 的可见 event；terminal event 输出后退出。合法 cursor 已消费 terminal 时成功空输出退出；run 尚未 terminal且暂时无新 event 时有界轮询但不打印 heartbeat。 |
| 安全/副作用 | 默认隐藏 reasoning/internal；`--include-internal` 需要与 RUN-003/RUN-006 相同的额外权限。读取不得创建或修改 run/event/audit outcome，不得形成隐藏/其他 run seq oracle。 |
| 背压 | 复用与 RUN-006 相同的单条 `65536` bytes 合法性、`100` event / `1048576` bytes page、一次一页与逐行写出边界；终端 stdout 阻塞时不得预取第二页。 |
| 验证要求 | CLI contract 覆盖默认/内部可见性、`0`/可见/隐藏/空洞/其他 run/越界 cursor、NDJSON canonical bytes、terminal/已消费 terminal、慢 stdout、Ctrl-C、legacy invalid row、SQLite 与真实 PostgreSQL reader；与 RUN-006 双向断言同一过滤和序列化结果。 |

## 7. Agent Registry API

### AGT-001 列出 agents

| 字段 | 内容 |
|---|---|
| Contract ID | `AGT-001` |
| 状态 | 已实现，提供 template route、OpenAPI schema、CLI 等价入口和认证可见性过滤。 |
| 入口 / 调用方 | OpenAPI 调用方、service-app、未来 Access/API gateway；CLI 等价入口为 `agent-harness agents list`。 |
| 用途 | 列出 registry 中已加载且通过校验的 agent public descriptor，供开发者、OpenAPI 调用方和后续管理面发现可运行 agent。 |
| 方法 | `GET` |
| 路径 | `/api/v1/agents` |
| 认证 | 已接入 `IdentityContext` dependency；local/dev 未配置 verifier 时注入默认身份，认证 profile 启用 verifier 时按 tenant/identity 可见性过滤 agent descriptor。 |
| 请求头 | 可选 `Accept: application/json`、`X-Request-Id`；认证 profile 启用 verifier 时必填 `Authorization`。 |
| Path 参数 | none |
| URL 参数 | none；后续如加分页或过滤必须先更新本文档和 drift tests。 |
| 请求体 | none |
| 幂等性 | 幂等读取；不得创建 run、checkpoint、event、trace 或 provider call；允许写入 `policy.decision` audit evidence。 |
| 副作用 | 读取 registry config 和验证结果；写入 policy 可见性检查 audit evidence。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`；不保证 `X-Request-Id` response header。 |
| 响应体 | `AgentListResponse` |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`409 registry.duplicate_agent_id`、`422 registry.invalid_config`、`500 api.internal_error`。 |
| 状态语义 | `agents=[]` 表示 registry 可用但当前没有 agent；`409/422` 表示 registry config 不可信，调用方不得把部分 descriptor 当作成功结果。 |
| 安全规则 | API 只返回当前身份可见的 public descriptor；不得暴露本地绝对路径、secret、provider client、callable、SQLAlchemy model 或 Python module object。重复 `agent_id` 或无效 config 必须整体拒绝 registry，不返回半成功列表。 |
| 验证要求 | `tests/contracts/test_agent_registry_model_context_contracts.py` 必须覆盖 OpenAPI path/method、`AgentListResponse` schema、`AgentDescriptor` 可见字段和禁止字段、`ApiErrorEnvelope` 错误 schema、重复 `agent_id`、registry validation error，以及 route 通过 `AgentRegistry` seam 而非直接读文件；认证/策略/HITL contract tests 还必须覆盖 401/403 和身份可见性过滤。 |

## 8. Auth / Policy / HITL API

### APR-001 列出 run approvals

| 字段 | 内容 |
|---|---|
| Contract ID | `APR-001` |
| 状态 | 已实现。 |
| 入口 / 调用方 | OpenAPI 调用方、HITL approval flow、CLI 等价入口 `agent-harness approvals list`、future Access/API gateway。 |
| 用途 | 列出当前身份可见的 run approval 记录，供人工处理等待审批的危险动作。 |
| 方法 | `GET` |
| 路径 | `/api/v1/runs/{run_id}/approvals` |
| 认证 | 必须注入 `IdentityContext`；当前身份必须能读取该 run 和审批。 |
| 请求头 | 可选 `Accept: application/json`、`X-Request-Id`；认证启用时必填 `Authorization`。 |
| Path 参数 | `run_id: string` |
| URL 参数 | `status: string` 可选；未传时返回当前身份可见的所有审批。 |
| 请求体 | none |
| 幂等性 | 幂等读取。 |
| 副作用 | 写入 approval list/read audit evidence；不得推进 run、创建 approval 或写 provider call。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `ApprovalListResponse` |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`500 api.internal_error`。 |
| 状态语义 | `approvals=[]` 表示该身份当前没有可见审批；不代表 run 不存在。 |
| 安全规则 | 只返回脱敏 approval 摘要；不得返回 resume token、原始危险 payload、secret 或内部 checkpoint object。 |
| 验证要求 | 认证/策略/HITL contract tests 必须覆盖 OpenAPI path/schema、401/403、request_id、空列表、waiting approval 可见性和 `ApiErrorEnvelope`。 |

CLI 等价入口 `agent-harness approvals list <run_id>` 必须输出稳定制表符摘要列：`approval_id`、`status`、`action`、`resource`、`reason`、`tenant_id`、`agent_id`、`run_id`、`trace_id`、`request_id`。

### APR-001A 读取单项 run approval

| 字段 | 内容 |
|---|---|
| Contract ID | `APR-001A` |
| 状态 | 已实现。 |
| 入口 / 调用方 | OpenAPI 调用方、HITL approval detail、service-app；CLI 当前通过 `approvals list`/resolve seam 使用同一 repository，不单独增加 detail 命令。 |
| 用途 | 按 `run_id` 与 `approval_id` 双重定位一条当前身份可见的脱敏 approval。 |
| 方法 | `GET` |
| 路径 | `/api/v1/runs/{run_id}/approvals/{approval_id}` |
| 认证 | 必须注入 `IdentityContext`；当前身份必须有 approval read 权限且属于同一 tenant。 |
| 请求头 | 可选 `Accept: application/json`、`X-Request-Id`；认证启用时必填 `Authorization`。 |
| Path 参数 | `run_id: string`、`approval_id: string`；二者归属必须一致。 |
| URL 参数 | none |
| 请求体 | none |
| 幂等性 | 幂等读取；允许写一次 read audit evidence，不得改变 approval/run 状态。 |
| 副作用 | 写 approval read audit evidence；不得 resolve approval、消费 resume token 或推进 run。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `ApprovalDetailResponse`，包含 `request_id` 与脱敏 `ApprovalPublicRecord`。 |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`422 validation_error`、`500 api.internal_error`。 |
| 状态语义 | 返回记录的 `status` 表示 waiting/approved/denied/cancelled；404 不得泄漏跨 tenant 或跨 run approval 是否存在。 |
| 安全规则 | 不返回 resume token、checkpoint state、原始危险 payload、secret、ORM model 或内部 approval resolution state。 |
| 验证要求 | contract tests 必须覆盖 path/method、`ApprovalDetailResponse`、request_id、401/403/404/422 error envelope、跨 run/tenant 拒绝和 read audit evidence。 |

### APR-002 resolve approval

| 字段 | 内容 |
|---|---|
| Contract ID | `APR-002` |
| 状态 | 已实现。approve/deny 原子仲裁、带 owner timeout/fencing id 的私有 resolution lease、`ApprovalGrant`、进程重启与硬退出恢复、唯一 execution claim、确定性 failed 与 needs-review 分支均由 contract tests 固定。 |
| 入口 / 调用方 | OpenAPI 调用方、HITL approval flow、CLI 等价入口 `agent-harness approvals approve <approval_id>` / `agent-harness approvals deny <approval_id>`、future Access/API gateway。 |
| 用途 | 对 waiting approval 原子仲裁 approve 或 deny；approve 通过私有 lease 恢复原 continuation，deny 阻止目标动作并按策略 fail / fallback run。 |
| 方法 | `POST` |
| 路径 | `/api/v1/runs/{run_id}/approvals/{approval_id}` |
| 认证 | 必须注入 `IdentityContext`；当前身份必须有审批权限。 |
| 请求头 | `Content-Type: application/json`；可选 `Accept: application/json`、`X-Request-Id`；认证启用时必填 `Authorization`。 |
| Path 参数 | `run_id: string`、`approval_id: string` |
| URL 参数 | none |
| 请求体 | `ApprovalResolveRequest` |
| 幂等性 | public resolve 非幂等；service approve仅在私有 `resolution_state=claimed`、`enqueue_pending|queued`、无tool claim且本次 reviewer/decision/规范化request hash与私有fingerprint一致时复用原lease/operation：pending补投，queued不重投。worker startup只恢复 fingerprint完整、无claim的 `claimed+enqueue_pending`。`execution_owned`过期无claim时仅matching真实APR-002可换新lease/new operation，并以本次request id建立新operation首次correlation；其他重复resolve仍返回既有409。 |
| 副作用 | deny 仍在API/repository原子取得仲裁，零lease/queue/DBOS/handler。service approve只写private lease/fingerprint/enqueue state并投递 `resume_approval` refs，public保持waiting，API不执行executor/tool；worker pickup CAS为 `execution_owned`并保存DBOS owner/ref后恢复。旧lease/operation按fencing fail closed；目标实现必须让确定性结果把 `approval.resolved` 与对应 terminal 写入 durable ordered outbox，resolution 先于 terminal，二者完成后才公开 resolution。 |
| 成功响应码 | local inline `200`；service approve queued/in-progress `202`；deny `200`。 |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `ApprovalResolveResponse` |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`409 approval.invalid_transition`、`409 approval.resolution_in_progress`、`409 approval.execution_needs_review`、`409 run.invalid_transition`、`422 validation_error`、`503 approval.enqueue_unavailable`、`500 api.internal_error`。approve lease 已存在时，并发 deny/第二个 public resolve 使用 `approval.resolution_in_progress`；execution claim 状态不确定且无结果时使用 `approval.execution_needs_review`。 |
| 状态语义 | `approved` 表示人工允许原动作执行，不保证动作执行成功；completed 或确定性 failed result 先产生唯一 approved resolution evidence，再产生对应 run terminal，二者 durable 后完成 public approved resolution。`denied` 表示原动作不得执行，并遵守同一 resolution-before-terminal 证据顺序。结果不确定时 public approval 保持 waiting，`run.status` 保持非伪造的 waiting/failed 摘要，private state 可进入 needs_review。该有序恢复目标未通过完整验证与代码审核前不得描述为已交付。 |
| 安全规则 | path 中的 `run_id` 必须与 approval 归属一致；错误 URL 不得推进其他 run。response、OpenAPI、event 和 audit 不得泄漏 resume token、private lease/internal state、arguments 原文、secret 或原始危险 payload。仲裁失败方只允许写 conflict audit，不得发布第二个有效 resolution event。 |
| 验证要求 | contract tests 必须覆盖 deny 先赢时 approve 无 lease且 handler 为零、approve lease 先赢时并发 deny 返回 in-progress 409且 public waiting不变、过期 raw claimed lease 由真实 APR-002 route 换发 fencing id并继续、未过期 lease与已有 claim不被抢占、旧 owner fencing失败、completed/确定性 failed 各自的 resolution-before-terminal 与最终 approved、executing-without-result 的 needs-review 409、重复 public resolve 409、跨 run拒绝、单一 resolution/audit、request_id和 OpenAPI 不公开 private state；还必须注入 approve/deny 的 `run.resumed`、`approval.resolved`、terminal sink 写前失败、写后确认丢失和进程重启，证明 outbox 使用稳定 event id按 resolution-before-terminal 恢复、public resolution不早于 prerequisite evidence、handler 0/1 次且 audit/resolution/terminal 各唯一。SQLite/PostgreSQL repository tests 均需覆盖 lease takeover、fencing、unique claim与 ordered outbox recovery。 |

### POL-001 policy check

| 字段 | 内容 |
|---|---|
| Contract ID | `POL-001` |
| 状态 | 已实现。 |
| 入口 / 调用方 | OpenAPI 调用方、CLI 等价入口 `agent-harness policy check`、runtime/tool/model/eval seam。 |
| 用途 | 对 actor/resource/action/context 执行 policy check，返回 allow、deny 或 require_approval。 |
| 方法 | `POST` |
| 路径 | `/api/v1/policies/check` |
| 认证 | 必须注入 `IdentityContext`；policy actor 从 `PermissionContext` 派生，不直接耦合认证实现。 |
| 请求头 | `Content-Type: application/json`；可选 `Accept: application/json`、`X-Request-Id`；认证启用时必填 `Authorization`。 |
| Path 参数 | none |
| URL 参数 | none |
| 请求体 | `PolicyCheckRequest` |
| 幂等性 | 逻辑幂等；允许写 audit evidence，但不得执行目标动作。 |
| 副作用 | 写 policy/audit evidence；`require_approval` 可返回 approval 摘要，但单独 policy check 不直接 resume run。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `PolicyDecisionResponse` |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`422 validation_error`、`500 api.internal_error`。 |
| 状态语义 | `allow` 可继续执行；`deny` 必须阻断目标动作；`require_approval` 必须进入 approval seam 或返回调用方处理。 |
| 安全规则 | context 只接收摘要和 refs；不得把完整 tool/retrieval/provider payload 或 secret 写入 request、response、event 或 audit。 |
| 验证要求 | 认证/策略/HITL contract tests 必须覆盖三态决策、YAML/DB provider seam、401/403、`ApiErrorEnvelope`、request_id 和 OpenAPI schema。 |

## 9. Tool Execution CLI / Runtime Seam

当前不新增工具执行 HTTP route。工具执行先通过 CLI、runtime module seam 和未来 worker seam 暴露，避免在 tool 安全边界未稳定前公开远程执行 API。后续若新增 `/api/v1/tools` 或等价 route，必须先按第 3 节补完整 endpoint 条目和 OpenAPI drift tests。

### 9.1 当前入口

| Contract ID | 状态 | 入口 / 调用方 | 用途 |
|---|---|---|---|
| `TLS-001` | 目标能力 | `agent-harness tools list`、runtime registry seam | 列出当前 actor/agent 可见的内置工具、FileTool、ShellTool 和 MCP discovery 工具摘要。 |
| `TLS-002` | 目标能力 | `agent-harness tools call`、runtime registry seam | 通过 `ToolRegistry` 执行一次受 policy 控制的工具调用，输出 `ToolCallResult`。 |
| `TLS-003` | 目标能力 | `agent_harness.tools.ToolRegistry` | 供 runtime、worker 和 template agent 通过 module seam 调用工具，不暴露 callable 或 vendor SDK object。 |

### 9.2 行为契约

| 字段 | 约束 |
|---|---|
| 认证 / 身份 | CLI 使用 profile 中的 `IdentityContext`；runtime/worker 必须传入已认证 actor。所有 mutating、shell、MCP、workspace 外访问和危险动作都进入 `PolicyEngine`。 |
| 请求 DTO | `ToolCallRequest`；CLI arguments 可来自 JSON 字符串或文件，但进入 registry 前必须转换成该 DTO 形状。 |
| 响应 DTO | `ToolCallResult`；CLI 输出可用文本或 JSON，但字段语义不得偏离 DTO。 |
| 幂等性 | 工具执行默认不幂等；调用方必须通过 run/trace/invocation id 关联审计。读文件和 list/search 是逻辑读操作，仍要记录 invocation evidence。 |
| 副作用 | FileTool 可读写 workspace；ShellTool 可启动受控子进程；MCP client 可连接配置 server。所有副作用必须先通过 schema validation、allowlist 和 policy。 |
| 持久化 | 工具执行边界必须持久化 `workspaces` 和 `tool_invocations`，至少记录 workspace root/policy ref、tool name、args_ref、result_ref、status、duration、tenant/run/agent/trace。大参数和结果走 artifact/ref。 |
| 安全规则 | 空 `agent.tool_allowlist` 表示无工具权限；workspace 外访问默认 deny 或 require_approval；ShellTool 默认 disabled，显式启用后仍必须拒绝越过 workspace 的路径参数和 symlink target；MCP tool 未 allowlist 时 policy 拒绝；secret 不进 inline result、event、audit 或 error。 |
| ContextAssembler | tool/MCP output 进入上下文前必须带 `source_ref`、`trust_level`、token/truncation metadata 和 artifact_ref，不允许裸字符串拼接。 |
| stdout/stderr refs | ShellTool 的大 stdout/stderr 必须写入 artifact store，并在 `ToolCallResult.result.stdout_ref` / `ToolCallResult.result.stderr_ref` 中返回 artifact ref；`artifact_ref` 只用于整体结果或非流式 payload。 |
| 错误码 | 工具 seam 必须使用第 5.21 节错误码；`ToolCallResult.error.code` 是调用方分支依据。 |
| OpenAPI | 当前 OpenAPI 不得出现未记录的 `/api/v1/tools` route；contract tests 要显式保护这一点。 |

### 9.3 验证要求

- Contract tests 必须检查 `API-Contract.md` 包含 `TLS-001`、`TLS-002`、`ToolCallRequest`、`ToolCallResult` 和“无新增 HTTP route”说明。
- CLI/runtime tests 必须覆盖 unknown tool、schema validation failure、policy deny、require_approval、空 agent tool allowlist、workspace 外路径、workspace 内 symlink 指向外部路径、`.agentignore`、ShellTool disabled、timeout、长 stdout/stderr artifact_ref、MCP allowlist denial 和 untrusted output metadata，并断言第 5.21 节错误码。
- Import-boundary tests 必须证明 MCP SDK 只出现在 `agent_harness.adapters.mcp` 或测试替身中。
- SQLite local migration tests 和 PostgreSQL service smoke 必须把 `workspaces`、`tool_invocations` 与既有 core schema 的证据分开报告。

## 10. P0 Runtime Completion Seams

### BGT-001 Parent execution-tree shared budget

| 字段 | 约束 |
|---|---|
| 状态 | 实现与完整门禁已完成；公开 HTTP shape 不变，内部 shared-budget seam 仍须在归档前通过 fresh 代码 1+2 与审查后收口。 |
| owner | 每个 root run 创建唯一同 tenant `ParentBudgetLedger`，`budget_owner_run_id=root.run_id` 且非空；该 root 的 direct model/embedding、delegation top-level claim 和 child allocation 共用同一 owner。P0 拒绝嵌套、孤儿、循环、跨租户或 delegation relation 不唯一的 topology。 |
| frozen snapshot | Root 创建时冻结 token/cost hard limits、cost-enabled 状态、registry/config/catalog versions，以及 root 和当时显式 targets 各自的 descriptor/model-policy/route/price sub-snapshot。Child 继承同一 owner snapshot ID/hard limits，只能按自己的 target sub-snapshot 进一步收紧已启用维度；reload 只影响新 root。 |
| typed secret | Tenant-scoped keyed request fingerprint 使用 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY` 或对应 `_FILE` typed setting。缺失、direct/file 冲突、越界、symlink、超限、非 UTF-8 或空值在 application startup fail closed；runtime/migration 不得直接读取 env/path，secret 原值不得进入 payload、snapshot、event、trace、audit、error 或数据库。 |
| identity / replay | Stable operation key 只定位 row；预算语义中的 `delegation_claim_id` 唯一等于既有 `AgentDelegation.id`，在 `budget_operation_claims` 与 `delegation_budget_allocations` 中持久化为 `delegation_id`，不得派生第二个 claim 标识。版本化 immutable identity、opaque keyed request fingerprint/key version、owner/snapshot/route/price/trusted bound 共同决定 exact replay 或 `budget.operation_conflict`。Exact replay 复用首次 reservation/result，不重新读取当前余额；conflict 在 ledger、relation、event capacity 与外部副作用前拒绝。 |
| reservation / settlement | 先形成受信有限 intent并通过 static hard eligibility，再执行 soft policy/fallback/approval，最后在 provider/child/queue 副作用前，以 owner ledger row lock/CAS 原子 reservation。Cost-disabled 合法 null/unavailable 不产生 cost impact；unknown、actual-over或 needs-review 保守占用并 fence 新 operation/terminal。Parent 只应用 delegation top-level claim 的差额，child allocation 不直接重复扣 parent aggregate。 |
| UoW 优先级 | `exact replay / identity conflict` → `authorization / owner / relation / snapshot` → `event.sequence_state_invalid` → `budget` → `event.sequence_exhausted` → unique-race reread。前序错误不得被后序 budget/capacity 错误覆盖；direct 公开为 `budget.reservation_rejected`，内部 reason 使用封闭枚举且不得泄漏动态余额。 |
| migration | `0016` 在 DDL 前扫描并验证全库 root/child topology。严格 `legacy_closed` tree 只保留旧历史；其他 tree 必须由与 backfill bundle 分离的 durable immutable source evidence 逐值证明 snapshot、identity、hash/version、ownership、route、trusted bound 与 child relation。不得使用 current config/default/reservation/actual/自证 bundle 推导；cost-enabled route 的必需 prices 必须非 null、非 bool、非负且有限。 |
| terminal / recovery | 未封闭 direct/delegation claim、child allocation、effect-started 无可信 result、unknown 或 needs-review 阻止 parent terminal。恢复只重用 durable claim/settlement 并补投稳定 outbox event，不重放 provider、child 或 queue 副作用。 |
| 公开边界 | P0 不新增 budget ledger HTTP route、公开 DTO 字段或动态余额查询。现有 agent budget 字段仅以本节明确的 shared hard-limit 语义解释。 |
| 验证 | SQLite/真实 PostgreSQL 覆盖混合并发、token/cost 双维、cost-disabled、replay/conflict、cache hit/miss、fallback/approval、三个 crash window、terminal fencing、full-topology migration 与独立 source evidence；拒绝路径 provider/child/queue 为零。 |

### DLG-001 受控 agent delegation

| 字段 | 约束 |
|---|---|
| 状态 | shared-budget 联合修复与完整门禁已完成；真实执行保持不变，预算 ownership、allocation、migration 与 terminal 仍须在同一冻结摘要上通过 fresh 代码 1+2。 |
| 入口 | runtime/worker 注册的内置 `agent.delegate` tool/module seam；P0 不新增公开 delegation HTTP endpoint。 |
| 请求 | parent `run_id`、source/target `agent_id`、child input、显式 idempotency key、IdentityContext、request/trace context。 |
| 策略 | 先校验 source descriptor 的 delegation edge，再执行 `agent.delegate` PolicyEngine check；任一步 deny 都不得创建 child run、queue message、provider call 或业务 CanonicalEvent；允许写一次脱敏 policy/audit denial evidence。 |
| 执行 | local profile 复用 orchestrator inline seam；service profile 复用 durable RunQueue。child `agent_runs.parent_run_id` 必须指向 parent，tenant/session/identity 不得由 child input 覆盖。 |
| 预算 | 遵循 BGT-001。Delegation top-level claim 与 root direct operation 竞争同一非空 owner ledger；child 在 top-level reservation 内取得 allocation，不另建 per-child ledger 或放大额度。ownership/edge/policy/tenant/cycle/depth 校验不创建 delegation/预算/child 业务状态；通过后，系统在同一事务中按 stable key/immutable identity exact replay 或创建 claim，同 identity 复用首次 reservation，异 identity 在预算写入前拒绝。预留在 child 创建前确定性失败时释放，创建后保持 durable并按可信 usage 结算；unknown/needs-review 不得当 0 或提前释放。 |
| 幂等 | 规范化 request hash 覆盖 tenant、identity、parent/source/target、child input 与稳定预算意图。P0 没有显式预算参数时，预算意图固定为 `inherit_parent`；hash 不得包含动态 parent 剩余额度、锁内计算的有效预留额或其他会在重试间变化的余额投影。新 idempotency claim 与首次有效 reservation 必须在同一事务提交或回滚；同 key 同 hash 只产生一个 claim/reservation/child run 并重放或恢复 durable 结果，即使其他 key 已改变 parent 余额也复用首次 reservation；同 key 异 hash 在 reservation 前返回 `delegation.idempotency_conflict`，且零 child/queue/provider/业务事件副作用。 |
| 结果 | 返回 `DelegationSummary`；parent 聚合只能读取已经通过非 bool、非负、有限数值和 cost-status 组合校验的持久化 child run、`ModelUsageEvidence` 和 trace refs，不能相信业务 agent 手填 summary或让负数反向冲减预算。 |
| 事件证据 | 获准请求在 parent run 上最多发布 `delegation.claimed` -> `delegation.child.created` -> `delegation.completed|delegation.failed` 三条 internal non-terminal CanonicalEvent；event id、payload、pre-child failure、needs_review 无 final、默认过滤与重放规则精确遵循 5.9，拒绝路径为零 delegation 业务事件。 |
| 错误 | `delegation.edge_denied`、`delegation.policy_denied`、`delegation.idempotency_conflict`、`delegation.cycle_detected`、`delegation.depth_exceeded`、`delegation.budget_exceeded`、`delegation.target_not_found`、`delegation.execution_failed`。 |
| 安全 | 错误、event、audit 与 tool result 必须脱敏；跨 tenant target、provider raw usage、resume token 和本地路径不得进入公开 summary。 |
| 验证 | deny 只允许 policy/audit evidence 且无 child side effect、allow/local、service queue、同 key 同/异 hash、同 key 并发与 claim 后崩溃恢复不重复 reservation、首次 claim 后由其他 key 改变 parent 余额再重试原 key 仍复用首次 reservation、不同 key 预算竞争、cycle/depth/budget、child failure、parent durable aggregation、trace/evidence refs 和 import boundary contract。 |

### MOD-001 model / embedding usage evidence

| 字段 | 约束 |
|---|---|
| 状态 | `ready-to-archive`；完整验证与代码 1+2 已通过，保持 active 且不自动归档。 |
| 入口 | provider adapter -> model/embedding router/facade -> EventBus/TelemetryFacade；业务 agent 只消费输出和稳定 DTO。 |
| 生命周期 | model 与 embedding 调用前都发布 `model.request.started`，完成或受控失败后都发布恰好一条调用级最终 `model.usage.updated`，并以 `ModelUsageEvidence.usage_kind` 区分；不得新增等价 embedding event type。`usage_call_id` 必须由 durable tenant/run/request/agent/trace 关联与稳定语义调用槽位生成，invocation seam 不提供随机回退，也不得把 prompt、embedding input 等敏感业务输入纳入 ID。`model.usage.updated` 必须 `CanonicalEvent.terminal=false`，run terminal marker 仍只属于三种 run terminal event。 |
| terminal 顺序 | 每次 started 调用先写 durable settlement/outbox 状态；provider 结果只写入该状态一次，再按稳定 `usage_call_id`/event id 幂等发布最终 usage。service worker 在 DBOS runtime 启动前恢复全部已有确定结果，queued run 重放或执行前再做 run-scoped recovery；恢复只补投 model/embedding evidence，不得误消费 approval 等共享 outbox 项。runtime 收口前恢复所有 pending settlement，最终 usage 必须先于同一 run terminal；不得重放 provider。`approval.resolved` 等其他前置 evidence 与 terminal 复用同一有序 outbox，EventBus/sink 拒绝 terminal 后的任何业务事件。 |
| 证据 | 每次调用产生 `ModelUsageEvidence`，包含 provider/model、token、cost 可用性、latency、route/fallback/cache/budget decision、run/agent/trace；所有数值在持久化和聚合前拒绝 bool、负数与非有限值。 |
| cost | `reported|estimated` 必须带非负有限 `cost_usd`，`unavailable` 必须为 null；provider 未返回且无可验证价目配置时不得写 0，估算值必须标 `estimated`。 |
| cache hit | embedding cache hit 仍发布 started/final evidence，记录本次 lookup latency、null token/cost、`unavailable` 与 `cache_status=hit/provider_called=false`；不复用首次 provider latency且 provider 调用次数为零。 |
| 持久化 | local JSONL 与 service PostgreSQL event sink 保留 provider-neutral 摘要；大/raw provider payload 不落事件正文。 |
| 安全 | secret、prompt 全文、embedding 原文、provider client/raw response 不得进入公开 DTO、event、trace 或 error。 |
| 验证 | 本 capability 验证 fake/model/embedding、reported/estimated/unavailable cost、fallback/policy required、失败脱敏、event seq/trace 关联和业务 agent import boundary。parent delegation aggregation 属于后序 `agent-delegation-execution` 及联合验收，不作为 MOD-001 的退出条件。 |

### CFG-001 Docker secret file 配置加载

| 字段 | 约束 |
|---|---|
| 状态 | 当前已实现（P0），包括异常链与 traceback frame locals 脱敏；保持既有 typed settings 边界，不引入 `SecretProvider` 抽象。 |
| 入口 | 进程环境中的 `<BASE_ENV>_FILE`，例如 `AGENT_HARNESS_STORAGE__DSN_FILE`；去掉 `_FILE` 后复用既有 typed env path 解析。 |
| 冲突 | 同时设置 `<BASE_ENV>` 与 `<BASE_ENV>_FILE` 必须结构化失败，不静默选择一个。 |
| 文件 | 必须是受信 secret root 内的绝对、普通、非 symlink、UTF-8、非空文件；限制最大 64 KiB，只移除一个结尾换行。默认 service root 为 `/run/secrets`，测试可显式注入临时受信 root。 |
| 合并顺序 | profile YAML -> agent YAML -> `.env` -> Docker secret file -> process env -> explicit overrides；冲突检查优先于 merge。 |
| 错误 | 文件拒绝返回 `config.secret_file_invalid`，direct/file 冲突返回 `config.secret_file_conflict`；两者均包含安全 field path 与修复提示，但不得包含文件内容、解析后的 secret 或受信 root 外绝对路径。 |
| 验证 | 成功加载、direct/file 冲突、相对路径、目录、symlink、越界、空文件、非 UTF-8、超限、日志/error/health/doctor redaction 和 application startup failure；Compose 的 application DSN 与 PostgreSQL password 均使用独立只读 secret file，`docker compose config` 不展开 secret 原值。 |
| shared-budget key | `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY` / `_FILE` 复用本 seam；该字段必须由 typed settings 注入 BGT-001 composition，禁止 runtime 通过 `os.environ`、`Path.read_text()` 或自定义 `.strip()` 旁路读取。 |

## 11. Eval Gate API

### EVL-001 draft eval case

#### EVL-001A 创建 draft eval case

| 字段 | 内容 |
|---|---|
| Contract ID | `EVL-001A` |
| 状态 | 已实现。 |
| 入口 / 调用方 | OpenAPI 调用方、CLI 等价入口 `agent-harness eval draft`、trace/score detector seam。 |
| 用途 | 从 failed/low-score trace 或人工输入创建待审 draft，不写 approved dataset。 |
| 方法 | `POST` |
| 路径 | `/api/v1/eval-cases/drafts` |
| 认证 | 需要有效 HTTP Bearer；actor/tenant 来自 `IdentityContext`，不得从 body 覆盖。 |
| 请求头 | `Content-Type: application/json`、`Authorization: Bearer <token>`；可选 `X-Request-Id`。 |
| Path 参数 | none |
| URL 参数 | none |
| 请求体 | `EvalDraftCreateRequest`：必填 `agent_id`；可选 `run_id`、`trace_id`、`trigger`、`input`、`output`、`expected`、`scores`、`score_threshold`、`source_refs`、`artifact_refs`、`metadata`。 |
| 幂等性 | 默认非幂等；相同 body 可创建不同 draft，调用方需要自行用 trace/case ref 去重。 |
| 副作用 | 写 draft eval case 和必要 audit/trace evidence；不得写 approved dataset、eval run 或 score。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `EvalCaseResponse`，包含 `request_id` 和脱敏 `EvalCaseRecord`，`status=draft`。 |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`422 validation_error` / secret-privacy scan failure、`500 api.internal_error`。 |
| 状态语义 | `status=draft` 表示必须人工 review；provider 状态不参与 draft 创建成败。 |
| 安全规则 | input/output/error 先脱敏；大 payload 只留 artifact/source refs；未认证时不得产生任何 case/audit/provider side effect。 |
| 验证要求 | contract tests 覆盖 path/method、HTTPBearer、request/response schema、request_id、draft-only、secret 422、401/403/422/500 `ApiErrorEnvelope` 和无 approved side effect。 |

#### EVL-001B 列出 draft eval cases

| 字段 | 内容 |
|---|---|
| Contract ID | `EVL-001B` |
| 状态 | 已实现。 |
| 入口 / 调用方 | OpenAPI 调用方、CLI 等价入口 `agent-harness eval list --status draft`、人工 review queue。 |
| 用途 | 列出当前 tenant 可见的 draft 摘要，可按 agent/dataset 过滤。 |
| 方法 | `GET` |
| 路径 | `/api/v1/eval-cases/drafts` |
| 认证 | 需要有效 HTTP Bearer 和 eval read 权限；tenant 由 identity 固定。 |
| 请求头 | `Authorization: Bearer <token>`；可选 `Accept`、`X-Request-Id`。 |
| Path 参数 | none |
| URL 参数 | `agent_id: string` 可选；`dataset: string` 可选。 |
| 请求体 | none |
| 幂等性 | 幂等读取。 |
| 副作用 | 只允许写 read audit evidence，不得修改 case。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `EvalCaseListResponse`，包含 `request_id` 与 `cases`；空列表表示没有匹配 draft。 |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`422 validation_error`、`500 api.internal_error`。 |
| 状态语义 | `cases=[]` 是成功空态，不等于 approved dataset 为空。 |
| 安全规则 | 只返回当前 tenant 脱敏摘要；不得返回 provider 原始对象或完整大 payload。 |
| 验证要求 | contract tests 覆盖 filters、空态、跨 tenant 隔离、request_id、HTTPBearer 和 401/403/422/500 error envelope。 |

### EVL-002 approve eval case

#### EVL-002A 人工 approve eval case

| 字段 | 内容 |
|---|---|
| Contract ID | `EVL-002A` |
| 状态 | 已实现。 |
| 入口 / 调用方 | OpenAPI 调用方、CLI 等价入口 `agent-harness eval approve <case_id>`、人工 review flow。 |
| 用途 | 把一条当前 tenant 的 draft 人工确认到指定 approved dataset。 |
| 方法 | `POST` |
| 路径 | `/api/v1/eval-cases/{case_id}/approve` |
| 认证 | 需要有效 HTTP Bearer、人工 reviewer identity 和 `eval.case.approve` policy allow；自动 detector 不得调用。 |
| 请求头 | `Content-Type: application/json`、`Authorization: Bearer <token>`；可选 `X-Request-Id`。 |
| Path 参数 | `case_id: string`。 |
| URL 参数 | none |
| 请求体 | `EvalApproveRequest`：必填非空 `reason`，可选 `dataset`，默认 `default`。 |
| 幂等性 | 非幂等；已 approved/invalid case 再 approve 返回冲突，不重复写 audit。 |
| 副作用 | 原子更新 case status/dataset/reviewer/reason，并写 audit；不得留下半个 approved case。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `EvalCaseResponse`，包含 `request_id`、approved case 和 `audit_ref`。 |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`409 eval.invalid_transition`、`422 validation_error`、`500 api.internal_error`。 |
| 状态语义 | 成功后 `case.status=approved`；失败时原 draft 保持可审阅。 |
| 安全规则 | reviewer 来自 identity；reason/case/audit 均脱敏；不得自动批准 detector 生成的 case。 |
| 验证要求 | contract tests 覆盖人工身份/policy、request_id/audit_ref、原子回滚、重复 approve、跨 tenant、HTTPBearer 和所有 error envelopes。 |

#### EVL-002B 列出 approved eval cases

| 字段 | 内容 |
|---|---|
| Contract ID | `EVL-002B` |
| 状态 | 已实现。 |
| 入口 / 调用方 | OpenAPI 调用方、CLI 等价入口 `agent-harness eval list --status approved`、eval runner。 |
| 用途 | 列出当前 tenant 的 approved dataset 摘要。 |
| 方法 | `GET` |
| 路径 | `/api/v1/eval-cases/approved` |
| 认证 | 需要有效 HTTP Bearer 和 eval read 权限。 |
| 请求头 | `Authorization: Bearer <token>`；可选 `Accept`、`X-Request-Id`。 |
| Path 参数 | none |
| URL 参数 | `agent_id: string` 可选；`dataset: string` 可选。 |
| 请求体 | none |
| 幂等性 | 幂等读取。 |
| 副作用 | 只允许 read audit evidence。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `EvalCaseListResponse`；`cases=[]` 表示成功空态。 |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`422 validation_error`、`500 api.internal_error`。 |
| 状态语义 | 返回记录必须全部为 approved；draft 永不混入。 |
| 安全规则 | tenant 过滤、脱敏与 artifact ref 规则同 EVL-001B。 |
| 验证要求 | contract tests 覆盖 approved-only、filters、空态、tenant 隔离、request_id、HTTPBearer 和 error envelope。 |

### EVL-003 run eval and read scores

#### EVL-003A 创建并运行 approved eval

| 字段 | 内容 |
|---|---|
| Contract ID | `EVL-003A` |
| 状态 | 已实现。 |
| 入口 / 调用方 | OpenAPI 调用方、CLI 等价入口 `agent-harness eval run`、`make eval`。 |
| 用途 | 对指定 agent/dataset 的 approved cases 创建 eval run 并写 score evidence。 |
| 方法 | `POST` |
| 路径 | `/api/v1/evals/runs` |
| 认证 | 需要有效 HTTP Bearer 和 eval run 权限；未认证不得创建 eval run/score/provider side effect。 |
| 请求头 | `Content-Type: application/json`、`Authorization: Bearer <token>`；可选 `X-Request-Id`。 |
| Path 参数 | none |
| URL 参数 | none |
| 请求体 | `EvalRunCreateRequest`：必填 `agent_id`，可选 `dataset`，默认 `default`。 |
| 幂等性 | 当前非幂等；每次成功请求创建新的 `eval_run_id`。 |
| 副作用 | 只读取 approved cases；写 eval run、score 和 local-first telemetry evidence，provider fan-out 可降级。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `EvalRunResponse`，含 `request_id`、`eval_run_id`、status、case_count、score_summary、local_refs、provider_statuses。 |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`422 validation_error`、`500 api.internal_error`。 |
| 状态语义 | approved 为空返回 `no_approved_cases` 和 case_count=0；不得执行 draft 或伪造 score。 |
| 安全规则 | score 先写脱敏 local evidence，再 fan-out；provider failure 只产生 degraded status，不删除 local refs。 |
| 验证要求 | contract tests 覆盖 approved-only、draft skip、empty dataset、local-first/provider degrade、request_id、HTTPBearer 和 401/403/422/500 error envelopes。 |

#### EVL-003B 读取 eval run

| 字段 | 内容 |
|---|---|
| Contract ID | `EVL-003B` |
| 状态 | 已实现。 |
| 入口 / 调用方 | OpenAPI 调用方、service-app、eval 调试工具。 |
| 用途 | 按 `eval_run_id` 读取当前 tenant 的 eval run 摘要。 |
| 方法 | `GET` |
| 路径 | `/api/v1/evals/runs/{eval_run_id}` |
| 认证 | 需要有效 HTTP Bearer 和同 tenant 可见性。 |
| 请求头 | `Authorization: Bearer <token>`；可选 `Accept`、`X-Request-Id`。 |
| Path 参数 | `eval_run_id: string`。 |
| URL 参数 | none |
| 请求体 | none |
| 幂等性 | 幂等读取。 |
| 副作用 | none；允许 read audit evidence。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `EvalRunResponse`，不得包含 provider client/raw response。 |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`422 validation_error`、`500 api.internal_error`。 |
| 状态语义 | `status`/case_count/score_summary/provider_statuses 共同表达 running/completed/degraded/no_approved_cases。 |
| 安全规则 | 跨 tenant 返回 404/403，不泄漏 run 是否存在；provider error 只返回脱敏摘要。 |
| 验证要求 | contract tests 覆盖 response schema、request_id、local refs、跨 tenant、404、HTTPBearer 和 error envelope。 |

#### EVL-003C 读取 eval scores

| 字段 | 内容 |
|---|---|
| Contract ID | `EVL-003C` |
| 状态 | 已实现。 |
| 入口 / 调用方 | OpenAPI 调用方、CLI 等价入口 `agent-harness eval scores`、人工 eval review。 |
| 用途 | 读取当前 tenant 可见 eval run 的 score evidence。 |
| 方法 | `GET` |
| 路径 | `/api/v1/evals/runs/{eval_run_id}/scores` |
| 认证 | 需要有效 HTTP Bearer 和同 tenant 可见性。 |
| 请求头 | `Authorization: Bearer <token>`；可选 `Accept`、`X-Request-Id`。 |
| Path 参数 | `eval_run_id: string`。 |
| URL 参数 | none |
| 请求体 | none |
| 幂等性 | 幂等读取。 |
| 副作用 | none；允许 read audit evidence。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `EvalScoresResponse`，含 `request_id` 和脱敏 `scores`；空数组表示尚无 score。 |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`422 validation_error`、`500 api.internal_error`。 |
| 状态语义 | `scores=[]` 是成功空态；不得据此推断其他 tenant 的 run。 |
| 安全规则 | 不返回 provider raw response、secret 或完整大 payload；只返回 score DTO 与 refs。 |
| 验证要求 | contract tests 覆盖 response schema、空态、request_id、tenant 隔离、404、HTTPBearer 和 error envelope。 |

### EVL-004 eval experiment and harness comparison

本条目是基础 draft / approve / run 链路之上的 trace/eval 升级契约，不改变已实现的人工审核基础语义；开工前必须先补 contract tests，再实现 route、CLI 和 storage schema。

| 项目 | 契约 |
|---|---|
| Method / Path | `POST /api/v1/evals/experiments`、`GET /api/v1/evals/experiments/{experiment_id}`、`GET /api/v1/evals/experiments/{experiment_id}/comparison`、`POST /api/v1/evals/experiments/{experiment_id}/accept` |
| 入口 / 调用方 | OpenAPI 调用方、CLI 等价入口 `agent-harness eval experiment create` / `show` / `compare` / `accept`、release gate 和 maintainer review flow。 |
| 身份 / 权限 | 创建和读取 experiment 需要有效 HTTP Bearer；accept 必须携带人工 reviewer 身份，并通过 policy/approval seam。未认证或未授权不得创建 experiment、eval run、accepted harness record 或 audit side effect。 |
| Request | create body 包含 `agent_id`、`dataset`、`tags`、`split_strategy`、`baseline_harness_version`、可选 `candidate_harness_version` / `optimization_ratio` / `holdout_ratio` / `regression_policy` / `metadata`。`tags` 必须来自 approved case metadata；draft case 不得参与 split。省略 candidate 时只创建不可变的 baseline snapshot，不支持在原 experiment 上后补；需要 comparison 时必须新建同时携带 baseline/candidate 的 experiment。 |
| Response | create/read 返回 `request_id`、`experiment_id`、`status`、`agent_id`、`dataset`、`tags`、`optimization_case_count`、`holdout_case_count`、baseline/candidate eval run refs、local evidence refs 和 provider degraded summary。单个 evaluator 列表合法但顶层或 per-case failure diff 派生合并后超过公共 100 项或 16 KiB 时，DTO 构造与 terminal 持久化前将公共 refs 压缩为 `db://eval-experiments/<id>` 真相引用；create/read/replay、comparison、CLI、provider payload 使用同一有界结果，完整 refs 留在本地 score summary。 |
| Comparison | comparison response 返回 per-tag `baseline_score`、`candidate_score`、`delta`、`holdout_delta`、`regressions`、`new_failures`、`fixed_failures`、`acceptance_recommendation`、稳定 `recommendation_reason_codes` 和脱敏 evidence refs；不得返回 provider 原始响应或完整大 payload。 |
| Accept | accept body 包含 `decision`、`reason`、可选 `followup_issue_ref`，`decision=accepted` 时必须包含 `accepted_harness_version`，rejected 时该字段必须为空。accepted version 必须等于该 experiment 已完成 comparison 的 candidate version；只有 policy 允许且全部门禁通过时，才可写 accepted production binding 和 audit log。rejected decision 只写不可变 review decision 与 audit，不产生 accepted production binding；任何 decision 都不得自动修改 prompt、tool description 或生产配置文件。 |
| 幂等性 | create 必须支持必填 `Idempotency-Key`；相同 key 和相同 body 返回同一 `experiment_id`，同 key 不同 body 返回 409。Split、experiment 与首个私有 execution claim 原子提交；活跃 `running` 重放不得重复 evaluator/provider。heartbeat 续租失败/异常、claim 过期、进程中断或 evaluator 已执行但 terminal 结果无法持久化时转 `needs_review`；terminal 写入必须原子校验 owner 与未过期租约，后续重放只返回该状态，不自动重跑不确定副作用。每个 experiment 只允许一条不可变 review decision；同 reviewer、同规范化 decision body 重试返回同一 decision record，不得重复写 audit，其他 reviewer 或不同 decision/version 冲突返回 409。 |
| 错误语义 | 标签不存在、split 后 holdout 为空、candidate harness 缺失、provider 写入失败或 comparison evidence 不完整时返回稳定 `ApiErrorEnvelope`、`needs_review` 或 degraded summary；provider failure 不得删除 local evidence。 |
| OpenAPI | operation 必须声明 `HTTPBearer` security，并按 EVL-004A-D 各自的适用错误集合声明 `ApiErrorEnvelope`；create 的 `Idempotency-Key` header 必须标记 required；accept endpoint 必须在 schema 中暴露人工 reviewer / policy decision / audit ref 字段。 |

#### EVL-004A 创建 eval experiment

| 字段 | 内容 |
|---|---|
| Contract ID | `EVL-004A` |
| 状态 | 已实现 |
| 入口 / 调用方 | OpenAPI、`agent-harness eval experiment create`、maintainer experiment flow。 |
| 用途 | 从 approved tagged cases 创建持久化 split，并同步执行 baseline 与可选 candidate experiment。 |
| 方法 | `POST` |
| 路径 | `/api/v1/evals/experiments` |
| 认证 | 有效 HTTP Bearer；tenant/actor 来自 `IdentityContext`。 |
| 请求头 | `Content-Type: application/json`、必填非空 `Idempotency-Key`、认证 profile 下必填 `Authorization: Bearer <token>`；可选 `X-Request-Id`。 |
| Path 参数 | none |
| URL 参数 | none |
| 请求体 | `EvalExperimentCreateRequest` |
| 幂等性 | 同 tenant、key、规范化 body 返回同一 experiment；同 key 不同 body 返回 409。创建 split、experiment 和首个 execution claim 必须原子；active replay 只读返回 `running`，过期或结果不确定的执行 fenced 地转 `needs_review`，两者都不新增 evaluator/provider call。 |
| 副作用 | 新请求写 split/experiment/eval evidence，可能调用 evaluator 与 provider；幂等 replay 不创建新 run 或 provider call。若 evaluator 已开始但 terminal 结果无法确认，不得自动重跑，必须保留 `needs_review` 供人工核查。 |
| 成功响应码 | 新建返回 `201`；幂等 replay 返回 `200`。 |
| 响应头 | 只保证 `Content-Type: application/json`；request correlation 以 body `request_id` 为准。 |
| 响应体 | `EvalExperimentResponse` |
| 错误响应码 | 401/403/404/409/422/500 `ApiErrorEnvelope`；未知标签、非法 split、空 holdout、幂等冲突使用稳定 error code。 |
| 状态语义 | `running` 表示持有有效私有 claim 的活跃执行；有 candidate 且两个 run 完成时为 `completed`，省略 candidate 时为只读 baseline snapshot；确定性 evaluator failure 为 `failed` 并保留有界结构化错误摘要与已有 local evidence；claim 过期、进程中断或 terminal 写入失败等无法证明副作用结果的情况为 `needs_review`，不得自动 takeover/replay evaluator/provider。 |
| 安全规则 | 只消费 approved、无 secret、标签完整且同 tenant/agent/dataset 的 case；不得内联完整 case/provider payload。 |
| 验证要求 | route/side-effect、幂等、tenant、split、secret/provider degraded contract tests 与 OpenAPI drift。 |

#### EVL-004B 读取 eval experiment

| 字段 | 内容 |
|---|---|
| Contract ID | `EVL-004B` |
| 状态 | 已实现 |
| 入口 / 调用方 | OpenAPI、`agent-harness eval experiment show`、maintainer review flow。 |
| 用途 | 读取当前 tenant 可见的 experiment 状态、subset counts、harness/run refs 和 provider 摘要。 |
| 方法 | `GET` |
| 路径 | `/api/v1/evals/experiments/{experiment_id}` |
| 认证 | 有效 HTTP Bearer；tenant visibility 来自 `IdentityContext`。 |
| 请求头 | 认证 profile 下必填 `Authorization: Bearer <token>`；可选 `Accept: application/json`、`X-Request-Id`。 |
| Path 参数 | `experiment_id: string`，跨 tenant 与不存在统一按 404 处理。 |
| URL 参数 | none |
| 请求体 | none |
| 幂等性 | 只读、幂等。 |
| 副作用 | none；不得触发 evaluator、provider 或 audit 写入。 |
| 成功响应码 | `200` |
| 响应头 | 只保证 `Content-Type: application/json`；request correlation 以 body `request_id` 为准。 |
| 响应体 | `EvalExperimentResponse` |
| 错误响应码 | 401/403/404/500 `ApiErrorEnvelope`。 |
| 状态语义 | 原样返回 persisted status；`needs_review` 表示执行结果不确定且禁止自动重跑，provider degraded 不隐藏 local refs。 |
| 安全规则 | 不泄漏其他 tenant 的存在性、version、score、case count 或 refs。 |
| 验证要求 | read/cross-tenant/404/no-side-effect contract tests 与 OpenAPI drift。 |

#### EVL-004C 读取 experiment comparison

| 字段 | 内容 |
|---|---|
| Contract ID | `EVL-004C` |
| 状态 | 已实现 |
| 入口 / 调用方 | OpenAPI、`agent-harness eval experiment compare`、maintainer review flow。 |
| 用途 | 读取已持久化的 per-tag、holdout、regression 与 failure diff evidence。 |
| 方法 | `GET` |
| 路径 | `/api/v1/evals/experiments/{experiment_id}/comparison` |
| 认证 | 有效 HTTP Bearer；tenant visibility 来自 `IdentityContext`。 |
| 请求头 | 认证 profile 下必填 `Authorization: Bearer <token>`；可选 `Accept: application/json`、`X-Request-Id`。 |
| Path 参数 | `experiment_id: string`，跨 tenant 与不存在统一按 404 处理。 |
| URL 参数 | none |
| 请求体 | none |
| 幂等性 | 只读、幂等。 |
| 副作用 | none；只读取 create 阶段已落盘的 comparison，不重新运行 evaluator/provider。 |
| 成功响应码 | `200` |
| 响应头 | 只保证 `Content-Type: application/json`；request correlation 以 body `request_id` 为准。 |
| 响应体 | `EvalExperimentComparisonResponse` |
| 错误响应码 | 401/403/404/409/500 `ApiErrorEnvelope`；candidate missing 或 local evidence 不完整返回 409。 |
| 状态语义 | provider refs 可在 degraded status 下缺失；local evidence 不完整时不得返回可接受结论。 |
| 安全规则 | 只返回聚合、脱敏 diff 与 refs，不返回完整 case/trace/provider raw response。 |
| 验证要求 | comparison/read-only/candidate missing/degraded/redaction contract tests 与 OpenAPI drift。 |

#### EVL-004D 记录人工 harness decision

| 字段 | 内容 |
|---|---|
| Contract ID | `EVL-004D` |
| 状态 | 已实现 |
| 入口 / 调用方 | OpenAPI、`agent-harness eval experiment accept`、maintainer review flow。 |
| 用途 | 在 comparison、candidate binding、policy 和人工 reviewer 门禁后记录唯一 decision。 |
| 方法 | `POST` |
| 路径 | `/api/v1/evals/experiments/{experiment_id}/accept` |
| 认证 | 有效 HTTP Bearer；reviewer/tenant 来自 `IdentityContext`，并执行 `eval.harness.accept` policy。 |
| 请求头 | `Content-Type: application/json`、认证 profile 下必填 `Authorization: Bearer <token>`；可选 `X-Request-Id`。 |
| Path 参数 | `experiment_id: string`，跨 tenant 与不存在统一按 404 处理。 |
| URL 参数 | none |
| 请求体 | `EvalExperimentAcceptanceRequest` |
| 幂等性 | 同 reviewer、同规范化 decision body 返回同一 decision；其他 reviewer 或 body/version 冲突返回 409。 |
| 副作用 | allow 且门禁通过时原子写 review decision/audit，accepted 另写 production binding；rejected 无 binding。deny/require_approval/mismatch 不写 decision/binding。 |
| 成功响应码 | 新 decision 与安全 replay 均返回 `200`。 |
| 响应头 | 只保证 `Content-Type: application/json`；request correlation 以 body `request_id` 为准。 |
| 响应体 | `EvalExperimentAcceptanceResponse` |
| 错误响应码 | 401/403/404/409/422/500 `ApiErrorEnvelope`；policy deny 403，require_approval、gate/version/decision conflict 409。 |
| 状态语义 | accepted 只有 `production_binding=true` 才表示可供后续人工发布流程引用；rejected 永不产生 binding。 |
| 安全规则 | version 必须等于已比较 candidate；不得自动改写 prompt、tool description 或任何生产配置。 |
| 验证要求 | policy/audit/atomicity/idempotency/version binding/side-effect counts/CLI 等价/OpenAPI contract tests。 |

## 12. Health API

### HLT-001 读取应用 health/capability 摘要

| 字段 | 内容 |
|---|---|
| Contract ID | `HLT-001` |
| 状态 | 已实现。 |
| 入口 / 调用方 | 公开 OpenAPI 调用方、service-app local/dev 启动探针、Docker Compose liveness；真实 service dependency 验证仍使用 `make smoke-service`。 |
| 用途 | 证明 FastAPI app 已启动，并返回当前 profile 的脱敏 storage/queue/observability capability 摘要。 |
| 方法 | `GET` |
| 路径 | `/api/v1/health` |
| 认证 | 无需凭据；这是公开只读 liveness/capability endpoint。不得因公开而扩大其他 `/api/v1` route 的认证范围。 |
| 请求头 | 可选 `Accept: application/json`、`X-Request-Id`。 |
| Path 参数 | none |
| URL 参数 | none |
| 请求体 | none |
| 幂等性 | 幂等读取。 |
| 副作用 | none；不得写库、探测外部 provider、创建 run/audit 或修改配置。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `HealthResponse`。 |
| 错误响应码 | app 已启动后的 route/dependency 异常返回 `500 api.internal_error`，公开 message 使用固定安全摘要，不返回 DSN/token/绝对路径。无效 profile 在 FastAPI app 创建前直接导致启动失败，因此不存在可请求的 health endpoint，也不得伪装成 HTTP 500。 |
| 状态语义 | `status=ok` 表示 app 与类型化 profile 装配成功；`degraded` 仅表示 capability 配置降级，不代表实时 network probe 失败。profile 装配失败属于进程启动门禁，不属于运行中 health 状态。 |
| 安全规则 | response 只允许 kind/status/profile/request_id；禁止 DSN、Redis URL、password、token、endpoint credential、token env value、本机绝对路径和 provider 原始对象。 |
| 验证要求 | contract tests 覆盖公开访问、X-Request-Id 透传、local/service summary、secret/绝对路径不泄漏、`HealthResponse` OpenAPI schema、500 `ApiErrorEnvelope`；service readiness 另由 PostgreSQL/Redis smoke 证明。 |

## 13. 入口 / 调用方映射

| 入口 / 调用方 | 当前或目标接口 | 说明 |
|---|---|---|
| `agent-harness run <agent_id> [--trace-id <value>]` | `CLI-RUN-001`，等价于 `RUN-001` 的 runtime seam | CLI 不走 HTTP，但必须使用同一 `RunOrchestrator`、storage、event bus、trace normalizer 和 DTO 语义。 |
| `agent-harness events stream <run_id>` | `CLI-EVT-001` stream seam | CLI 不走 HTTP/SSE framing；必须复用 RUN-003/RUN-006 的授权 EventSink reader、visibility、cursor、canonical serializer 和 terminal 语义。 |
| `agent-harness agents list` | 等价于 `AGT-001` 的 registry seam | CLI 不走 HTTP，但必须使用同一 `AgentRegistry`、descriptor DTO、identity/policy visibility 和 validation 语义。 |
| `agent-harness policy check` | 等价于 `POL-001` 的 policy seam | CLI 不走 HTTP，但必须使用同一 `PolicyEngine`、identity、audit 和 decision DTO。 |
| `agent-harness approvals list/approve/deny` | 等价于 `APR-001` / `APR-002` 的 approval seam | CLI 不走 HTTP，但必须使用同一 `ApprovalService`、runtime resume 和 audit seam。 |
| `agent-harness tools list/call` | 等价于 `TLS-001` / `TLS-002` 的 tool execution seam | CLI 不走 HTTP，但必须使用同一 `ToolRegistry`、PolicyEngine、workspace guard、artifact store、audit 和 DTO 语义。 |
| runtime / worker tool call | 等价于 `TLS-003` 的 module seam | runtime/worker 必须通过 `ToolRegistry`，不得直接调用 FileTool、ShellTool、MCP SDK、subprocess 或文件系统危险操作。 |
| runtime / worker delegation | `DLG-001` module seam | 通过内置 `agent.delegate` 复用 registry、PolicyEngine、orchestrator/RunQueue、storage/event；P0 不新增远程 delegation route。 |
| runtime / worker shared budget | `BGT-001` module seam | direct model/embedding、delegation 与 child allocation 复用同一 owner ledger/UoW；P0 不新增 budget ledger route 或公开动态余额。 |
| model / embedding adapter | `MOD-001` evidence seam | adapter 必须输出 provider-neutral `ModelUsageEvidence`，并通过 EventBus/TelemetryFacade 关联 run/trace；业务 agent 不拼 raw usage。 |
| service config loader | `CFG-001` settings seam | `<BASE_ENV>_FILE` 只在受控 typed settings 边界读取，包括 BGT-001 keyed fingerprint secret；P0 不引入 SecretProvider。 |
| OpenAPI 调用方 | 当前 `AGT-001`、`RUN-001` 到 `RUN-005`；P0 待实现 `RUN-006` | `/docs`、`/redoc`、`/openapi.json` 是当前版本管理面，不是前端 SaaS UI。 |
| service-app FastAPI | 当前 `AGT-001`、`RUN-001` 到 `RUN-005`；P0 待实现 `RUN-006` | route module 保持薄层，app factory 负责依赖注入、lifecycle 和 error handler。 |
| runtime worker | 当前 service profile 独立进程；不暴露 HTTP 管理面 | worker 通过 runtime components消费 Redis queue，使用稳定 DBOS executor id并从 PostgreSQL恢复 execution identity/checkpoint；不直接泄漏 ORM/DBOS/provider对象。 |
| HITL approval flow | `APR-001` / `APR-002` + runtime 内部 resume seam | approval continuation 必须关联 checkpoint、audit、tenant、identity、agent、run、action/resource 和 arguments hash；公开 `RUN-005` 只服务普通 checkpoint，不能执行 approval-gated 动作。 |
| Eval review / experiment flow | `EVL-*` | draft 到 approved 必须人工确认，secret/隐私脱敏是写入门禁；experiment accept 必须有人审、policy/audit 和回归证据。 |
| 当前 API/worker split | 所有 HTTP API + worker seam | API 与 worker 已物理分进程；数据只走 DTO、CanonicalEvent、repository/provider/facade，不传进程内可变对象；queue message必须携带 `request_id`、effective `idempotency_key`、`tenant_id`、`run_id`。下一步才是 tool/model gateway，再后是 observability/event pipeline；storage service仍待 repository contract 稳定。 |

## 14. 流式与事件契约

当前实现：

- `GET /api/v1/runs/{run_id}/events` 返回 JSON `RunEventsResponse`。
- 该 route 按 `after_seq` 读取 `CanonicalEvent`，不是 SSE 握手 endpoint。

P0 待实现 SSE 与 P1 可选 WS：

- SSE 是 P0 Access 层输出协议，WS 是 P1 可选 adapter；二者都不能替代内部 `CanonicalEvent` 模型。
- SSE event 必须由 `CanonicalEvent` 显式映射，保留 `seq`、`event_type`、`terminal`、`visibility` 和适用的 `trace_id/request_id`。
- SSE adapter 必须把客户端 `Last-Event-ID` 映射为 `CanonicalEvent.seq` 续读起点；JSON events seam 继续使用 `after_seq`。
- 断线恢复必须以 `seq` 为准；final 结算以 terminal event 为准。
- 握手前错误走 `ApiErrorEnvelope`；握手后错误必须转成可序列化 event，且不得泄露 secret/provider 原始错误。
- P0 endpoint、header、content type、可见性和性能验收以 `RUN-006` 为准；未实现前不得把 formatter 或 RUN-003 JSON route 标成 transport 已完成。

## 15. OpenAPI 生成与漂移检查

当前必须保留：

- FastAPI app 暴露 `/openapi.json`、`/docs`、`/redoc`。
- OpenAPI paths 必须包含当前已实现 run routes。
- `RunCreateResponse` schema 必须包含 `request_id`。
- `RunCreateResponse` schema 不得包含 `resume_token`。
- `ApiErrorEnvelope` 必须出现在已声明错误响应中。

已解决漂移与后续变更门禁：

- `run-openapi-contract-accuracy` 已移除 run router 级共享 `responses`，当前 RUN-002/003 等 operation 使用与生产路径一致的 operation-specific response map；局部 drift test 持续拒绝重新引入不可能返回的状态码。
- RUN-002 已由 `agent-delegation-execution` 切换到 `RunDetailResponse`；局部 drift test 必须拒绝退回 `RunCreateResponse`。
- drift test 不仅检查必需状态存在，还必须拒绝 contract 未声明的额外 response status，避免共享 router metadata 扩张公开契约。

每个新增或修改 endpoint 的开发门禁：

1. 先更新 `API-Contract.md` 对应 endpoint 条目。
2. 再新增或更新 contract tests，至少检查：
   - route path + method 存在；
   - request/response schema 名称和关键字段；
   - error envelope；
   - auth/visibility/security 状态；
   - 不存在旧的错误 alias route。
3. 最后实现 route 和 runtime seam。
4. 对应计划项收口时运行局部 tests；发布前再做全量 OpenAPI drift 复扫。

推荐局部验证入口：

```bash
uv run pytest tests/contracts/test_runtime_checkpoint_runs_contracts.py -q
```

新增 `approvals` 和 `policies` route 时，应按认证、策略、HITL 能力边界拆出对应 contract tests，不把所有 OpenAPI 检查堆进一个大测试。`agents` route 使用 `tests/contracts/test_agent_registry_model_context_contracts.py` 单独覆盖，认证/策略/HITL 还要补 401/403 和可见性检查。

## 16. 契约验收清单

- [x] 已区分当前已实现 run API 与保留 API。
- [x] 已按架构图映射 Access、Runtime、Engine、Tools、Infra、Eval Gate、Observability 和部署拆分边界。
- [x] 当前 run API 的 method、path、request、response、错误 envelope、幂等性、副作用和安全规则与运行 OpenAPI 精确一致，并由局部双向 drift contract 持续校验。
- [x] 已明确当前 events JSON seam、P0 待实现 RUN-006 SSE 与 P1 可选 WS 的边界。
- [ ] 已固定并实现 BGT-001、DLG-001、MOD-001、CFG-001 的输入、错误、安全、副作用和验证边界；typed fingerprint secret、`0016` topology/source/price、usage 错误优先级修正与完整门禁已完成，仍须按同一冻结摘要通过 fresh 代码 1+2 与审查后收口。
- [x] 已明确 `reasoning.delta` 默认不可见。
- [x] 已明确 API route 不得暴露 ORM、DBOS、provider SDK 或进程内 handle。
- [x] 已明确新增/修改 endpoint 必须先改本契约，再做局部 OpenAPI drift 检查。
- [x] Agent Registry 开工前补全 `AGT-001` 的完整 endpoint 条目和 contract tests。
- [x] Auth / Policy / HITL 开工前补全 auth、policy、approval endpoint 条目和 contract tests 目标。
- [x] Tool execution 开工前补全 tools CLI/runtime/module seam、无新增 HTTP route 和 contract tests 目标。
- [x] Eval Gate 开工前补全 eval endpoint 条目和 contract tests。
- [x] Trace/eval 升级开工前补全 eval experiment、harness comparison、acceptance gate endpoint 条目和 contract tests 目标。
- [x] Service App 基础表面已完成首轮全量 OpenAPI drift 复扫，并统一验证所有适用 operation 的 422 `ApiErrorEnvelope`。
- [x] Executor、approval continuation 和 scaffold 全部合入后，已针对更新后的 `APR-002`、完整 P0 CLI composition 和最终 OpenAPI 完成组合漂移复扫。
