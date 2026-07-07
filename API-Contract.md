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
| `artifacts/pydantic-ai-agent-architecture.drawio` | 5 层运行中轴、SSE/WS 回边、HITL 回路、信任边界、部署拆分边界。 |
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
- 当前版本不定义完整 SaaS 前端 URL，也不提供登录、注册、组织邀请或计费页面。

### 4.2 认证与身份

当前状态：

- 已有 `IdentityContext` / `PermissionContext` 和 default tenant/user contract。
- 当前已实现 run routes 还没有 FastAPI auth dependency；local/template 路径用于开发、测试和 smoke。

目标状态：

- 认证能力落地后，除明确的 health/local dev seam 外，mutating API 必须支持 API Key / Bearer Token。
- 认证层必须注入 `IdentityContext`，未启用多租户时使用默认 `tenant_id="default"`。
- 无效 token 调用受保护 API 必须返回认证错误且不创建 run、approval、eval case 或 audit side effect。

### 4.3 通用请求头

| Header | 必填 | 说明 |
|---|---:|---|
| `Accept` | No | 默认 `application/json`；未来 SSE endpoint 使用 `text/event-stream`。 |
| `Content-Type` | Conditional | JSON mutating request 使用 `application/json`。 |
| `X-Request-Id` | No | 调用方可传；服务端没有收到时生成 UUID，并写入响应 body 的 `request_id`。 |
| `Authorization` | 认证能力落地后 Conditional | `Bearer <token>` 或等价 API key 方案。当前已实现 run routes 尚未接入。 |

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
- 当前 tests 已覆盖 404/500 走 `ApiErrorEnvelope`；422 validation error 的 envelope 统一属于后续 API 完整化检查项。

### 4.6 通用状态码

| 状态码 | 用途 |
|---:|---|
| 200 | 同步读取或同步操作成功；当前 run create/cancel/resume 也返回 200。 |
| 201 | 未来同步创建资源成功时可用；使用前必须更新本契约。 |
| 202 | 未来异步排队成功时可用；必须返回 task/run id。 |
| 204 | 成功且无响应体。 |
| 400 | 请求语义错误或 HTTPException 400。 |
| 401 | 未认证或 token 无效，认证能力落地后适用。 |
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
  "terminal_event": "run.completed",
  "resume_token": "resume_123"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `request_id` | string | Yes | API 请求关联 ID。 |
| `run_id` | string | Yes | run 稳定 ID。 |
| `status` | `RunStatus` | Yes | 当前 run 状态。 |
| `terminal_event` | string | No | terminal run event，例如 `run.completed`。非 terminal 状态可为空。 |
| `resume_token` | string | No | run 处于 waiting/checkpoint 状态时可返回。调用方不得解析 token 格式。 |

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
  "payload": {
    "status": "completed"
  },
  "terminal": true,
  "visibility": "internal"
}
```

硬约束：

- 同一 `run_id` 内 `seq` 单调递增。
- terminal event 只能有一个，类型为 `run.completed`、`run.failed` 或 `run.cancelled`。
- `reasoning.delta` 默认不对普通用户可见。
- 大 payload 必须使用 `payload_ref`，并保留 checksum 或 artifact reference。

## 6. Run API

### RUN-001 创建 agent-scoped run

| 字段 | 内容 |
|---|---|
| Contract ID | `RUN-001` |
| 状态 | 已实现 |
| 入口 / 调用方 | OpenAPI 调用方、service-app、未来 Access/API gateway；CLI 等价入口为 `agent-harness run <agent_id>` |
| 用途 | 为指定 agent 创建一次 run，并通过 runtime seam 写入 run lifecycle 和 events。 |
| 方法 | `POST` |
| 路径 | `/api/v1/agents/{agent_id}/runs` |
| 认证 | 当前 local/template route 未接入 auth；认证能力落地后除 local dev seam 外应要求 API Key / Bearer。 |
| 请求头 | `Content-Type: application/json`；可选 `Accept: application/json`、`X-Request-Id`；认证能力落地后可选/必填 `Authorization` 按环境配置。 |
| Path 参数 | `agent_id: string`，稳定 agent ID。Agent Registry 能力落地后必须由 `AgentRegistry` 校验存在性和重复性。 |
| URL 参数 | none |
| 请求体 | `AgentRunCreateRequest` |
| 幂等性 | body 含 `idempotency_key` 时，同一 tenant/agent/session 下重复提交返回同一 run；缺失时非幂等。 |
| 副作用 | 写 run state、checkpoint/events；可能触发 model/tool/policy/worker 后续动作。当前 fake run 可同步完成或进入 waiting。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`；不保证 `X-Request-Id` response header。 |
| 响应体 | `RunCreateResponse` |
| 错误响应码 | `400 api.http_error`、`404 api.not_found`、`409 run.invalid_transition`、`422 validation_error`、`500 api.internal_error`。其中 422 envelope 统一仍需后续补验证。 |
| 状态语义 | `completed/failed/cancelled` 表示 terminal；`waiting` 表示调用方需要 approval 或 resume；`running/created` 表示后续通过 events/detail 追踪。 |
| 安全规则 | API route 不得直接操作 ORM session、DBOS API 或 provider SDK；input 进入 runtime 前必须经过 guardrail/trust 标注；认证能力落地后无效 token 不得创建 run。 |
| 验证要求 | `tests/contracts/test_runtime_checkpoint_runs_contracts.py` 必须检查 route table、OpenAPI path、helper 使用 `RunOrchestrator`、idempotency、request_id 和 error envelope。 |

### RUN-002 读取 run detail

| 字段 | 内容 |
|---|---|
| Contract ID | `RUN-002` |
| 状态 | 已实现 |
| 入口 / 调用方 | OpenAPI 调用方、service-app、未来 Access/API gateway。 |
| 用途 | 按 `run_id` 读取 run 当前状态，不暴露 ORM model 或内部 handle。 |
| 方法 | `GET` |
| 路径 | `/api/v1/runs/{run_id}` |
| 认证 | 当前 local/template route 未接入 auth；认证能力落地后按 tenant/identity 可见性检查。 |
| 请求头 | 可选 `Accept: application/json`、`X-Request-Id`；认证能力落地后可选/必填 `Authorization` 按环境配置。 |
| Path 参数 | `run_id: string` |
| URL 参数 | none |
| 请求体 | none |
| 幂等性 | 幂等读取。 |
| 副作用 | none。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `RunCreateResponse` |
| 错误响应码 | `404 api.not_found`、`500 api.internal_error`；认证能力落地后增加 `401/403`。 |
| 状态语义 | 调用方根据 `status` 和 `terminal_event` 判断继续轮询、读取 events、resume、cancel 或展示终态。 |
| 安全规则 | 认证能力落地后，非当前 tenant 或不可见 run 必须返回 `404` 或 `403`，不能泄漏其他 tenant 的 run 是否存在。 |
| 验证要求 | OpenAPI schema 必须包含 `request_id`；404 必须走 `ApiErrorEnvelope`。 |

### RUN-003 读取 run events

| 字段 | 内容 |
|---|---|
| Contract ID | `RUN-003` |
| 状态 | 已实现为 JSON event read seam；SSE/WS adapter 属于后续输出协议，不是当前 route。 |
| 入口 / 调用方 | OpenAPI 调用方、service-app、未来 SSE adapter、debug 工具、worker smoke。 |
| 用途 | 按 `seq` 读取 `CanonicalEvent`，供断线恢复、debug、SSE/API resume 共用。 |
| 方法 | `GET` |
| 路径 | `/api/v1/runs/{run_id}/events` |
| 认证 | 当前 local/template route 未接入 auth；认证能力落地后按 tenant/identity/event visibility 检查。 |
| 请求头 | 可选 `Accept: application/json`、`X-Request-Id`；认证能力落地后可选/必填 `Authorization` 按环境配置。 |
| Path 参数 | `run_id: string` |
| URL 参数 | `after_seq: integer >= 0`，默认 `0`；`include_internal: boolean`，默认 `false`。 |
| 请求体 | none |
| 幂等性 | 幂等读取；同一 `after_seq` 可重复读取同一事件窗口。 |
| 副作用 | none。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。未来 SSE route 必须使用 `text/event-stream`，不能复用本 JSON route 伪装成 SSE。 |
| 响应体 | `RunEventsResponse` |
| 错误响应码 | `404 api.not_found`、`422 validation_error`、`500 api.internal_error`；认证能力落地后增加 `401/403`。 |
| 状态语义 | 空数组表示当前没有新事件，不等于 run 已结束；terminal event 的 `terminal=true` 才是最终结算信号。 |
| 安全规则 | `include_internal=false` 时必须过滤 `reasoning.delta`；认证能力落地后 `include_internal=true` 需要权限。 |
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
| 认证 | 当前 local/template route 未接入 auth；认证能力落地后需要 run 操作权限。 |
| 请求头 | 可选 `Accept: application/json`、`X-Request-Id`；认证能力落地后可选/必填 `Authorization`。 |
| Path 参数 | `run_id: string` |
| URL 参数 | none |
| 请求体 | none |
| 幂等性 | 对非 terminal run 非幂等；对已 terminal run 必须返回 `409 run.invalid_transition`，不得改写终态。 |
| 副作用 | 更新 run status，写 terminal cancel event 和 audit/event evidence。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `RunCreateResponse` |
| 错误响应码 | `404 api.not_found`、`409 run.invalid_transition`、`500 api.internal_error`；认证能力落地后增加 `401/403`。 |
| 状态语义 | 成功后 `status=cancelled`，`terminal_event=run.cancelled`。 |
| 安全规则 | 取消动作不得绕过 policy/audit；后续 worker 分进程时必须通过 runtime seam 协调，不直接杀进程作为唯一状态来源。 |
| 验证要求 | runtime transition tests 必须证明 terminal run 不能再取消。 |

### RUN-005 resume run

| 字段 | 内容 |
|---|---|
| Contract ID | `RUN-005` |
| 状态 | 已实现 checkpoint resume seam；完整 HITL approval resume 依赖后续认证/审批能力。 |
| 入口 / 调用方 | OpenAPI 调用方、HITL approval flow、future worker/API gateway。 |
| 用途 | 使用 resume token 恢复 checkpointed run。 |
| 方法 | `POST` |
| 路径 | `/api/v1/runs/{run_id}/resume` |
| 认证 | 当前 local/template route 未接入 auth；认证能力落地后需要 run 操作权限和 approval/policy 检查。 |
| 请求头 | `Content-Type: application/json`；可选 `Accept: application/json`、`X-Request-Id`；认证能力落地后可选/必填 `Authorization`。 |
| Path 参数 | `run_id: string` |
| URL 参数 | none |
| 请求体 | `RunResumeRequest` |
| 幂等性 | 非幂等；token 已消费或 run 已 terminal 时不得推进状态。 |
| 副作用 | 解析 resume token、推进 run state、写后续 events；可能触发 worker/model/tool 后续动作。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `RunCreateResponse` |
| 错误响应码 | `404 api.not_found`、`409 run.invalid_transition`、`422 validation_error`、`500 api.internal_error`；认证能力落地后增加 `401/403`。 |
| 状态语义 | 成功后返回新的 run status；如果完成则返回 terminal event。 |
| 安全规则 | `resume_token` 必须属于 path 中的 `run_id`；错误 URL 不得推进其他 run。认证能力落地后 token 还必须匹配 tenant/identity/approval context。 |
| 验证要求 | contract tests 必须覆盖 token/run_id mismatch 先失败且不推进任一 run。 |

## 7. 保留 API 索引

这些路径来自 `Product-Spec.md` 的当前版本 API 列表。它们不是当前已实现能力；对应计划项开工前必须先把本节扩展成第 3 节规定的完整 endpoint 条目，再写 route。

| Contract ID | 状态 | 计划归属 | 路径 | 契约门禁 |
|---|---|---:|---|---|
| `AGT-001` | 规划中 | Agent Registry | `/api/v1/agents` | 必须定义 list schema、agent descriptor 可见字段、重复 `agent_id` 错误、registry config validation error。 |
| `APR-001` | 规划中 | Auth / Policy / HITL | `/api/v1/runs/{run_id}/approvals` | 必须定义 approval list/create/read 语义、policy decision、audit 字段、waiting run 关联。 |
| `APR-002` | 规划中 | Auth / Policy / HITL | `/api/v1/runs/{run_id}/approvals/{approval_id}` | 必须定义 approve/deny 方法、状态冲突、审批人身份、resume 触发规则。 |
| `EVL-001` | 规划中 | Eval Gate | `/api/v1/eval-cases/drafts` | 必须定义 draft list/create/review schema、secret scan、trace source、不可自动进入 approved。 |
| `EVL-002` | 规划中 | Eval Gate | `/api/v1/eval-cases/approved` | 必须定义 approved dataset 写入权限、人工确认、audit 和回滚/归档规则。 |
| `EVL-003` | 规划中 | Eval Gate | `/api/v1/evals/runs` | 必须定义 eval run create/detail/list schema、score sink、provider failure 降级。 |
| `POL-001` | 规划中 | Auth / Policy / HITL | `/api/v1/policies/check` | 必须定义 actor/resource/action/context request、allow/deny/require_approval response、audit policy。 |
| `HLT-001` | 规划中 | Service App / Service Profile | `/api/v1/health` | 必须定义 local/service profile health 字段、storage/queue/observability 状态和公开性。 |

## 8. 入口 / 调用方映射

| 入口 / 调用方 | 当前或目标接口 | 说明 |
|---|---|---|
| `agent-harness run <agent_id>` | 等价于 `RUN-001` 的 runtime seam | CLI 不走 HTTP，但必须使用同一 `RunOrchestrator`、storage、event bus 和 DTO 语义。 |
| OpenAPI 调用方 | `RUN-001` 到 `RUN-005`，后续保留 API | `/docs`、`/redoc`、`/openapi.json` 是当前版本管理面，不是前端 SaaS UI。 |
| service-app FastAPI | `RUN-001` 到 `RUN-005` | route module 保持薄层，app factory 负责依赖注入、lifecycle 和 error handler。 |
| runtime worker | 内部 worker seam；不直接新增 HTTP route | worker 必须通过 runtime components，不直接操作 ORM/DBOS/provider SDK。 |
| HITL approval flow | `RUN-005` + `APR-*` | approval/resume 必须关联 checkpoint、audit、tenant、run、identity。 |
| Eval review flow | `EVL-*` | draft 到 approved 必须人工确认，secret/隐私脱敏是写入门禁。 |
| future API/worker split | 所有 HTTP API + worker seam | 拆分后数据只走 DTO、CanonicalEvent、repository/provider/facade，不传进程内可变对象。 |

## 9. 流式与事件契约

当前实现：

- `GET /api/v1/runs/{run_id}/events` 返回 JSON `RunEventsResponse`。
- 该 route 按 `after_seq` 读取 `CanonicalEvent`，不是 SSE 握手 endpoint。

未来 SSE/WS adapter：

- SSE/WS 是 Access 层输出协议，不能替代内部 `CanonicalEvent` 模型。
- SSE event 必须由 `CanonicalEvent` 显式映射，保留 `seq`、`event_type`、`terminal`、`visibility` 和适用的 `trace_id/request_id`。
- 断线恢复必须以 `seq` 为准；final 结算以 terminal event 为准。
- 握手前错误走 `ApiErrorEnvelope`；握手后错误必须转成可序列化 event，且不得泄露 secret/provider 原始错误。

## 10. OpenAPI 生成与漂移检查

当前必须保留：

- FastAPI app 暴露 `/openapi.json`、`/docs`、`/redoc`。
- OpenAPI paths 必须包含当前已实现 run routes。
- `RunCreateResponse` schema 必须包含 `request_id`。
- `ApiErrorEnvelope` 必须出现在已声明错误响应中。

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

后续新增 `agents`、`approvals`、`evals`、`policies` 或 `health` route 时，应按功能拆出对应 contract tests，而不是把所有 OpenAPI 检查堆进一个大测试。

## 11. 契约验收清单

- [x] 已区分当前已实现 run API 与保留 API。
- [x] 已按架构图映射 Access、Runtime、Engine、Tools、Infra、Eval Gate、Observability 和部署拆分边界。
- [x] 已固定当前 run API 的 method、path、request、response、错误 envelope、幂等性、副作用和安全规则。
- [x] 已明确 events JSON seam 与未来 SSE/WS adapter 的边界。
- [x] 已明确 `reasoning.delta` 默认不可见。
- [x] 已明确 API route 不得暴露 ORM、DBOS、provider SDK 或进程内 handle。
- [x] 已明确新增/修改 endpoint 必须先改本契约，再做局部 OpenAPI drift 检查。
- [ ] Agent Registry 开工前补全 `AGT-001` 的完整 endpoint 条目和 contract tests。
- [ ] Auth / Policy / HITL 开工前补全 auth、policy、approval endpoint 条目和 contract tests。
- [ ] Eval Gate 开工前补全 eval endpoint 条目和 contract tests。
- [ ] Service App / Service Profile 收口时做全量 OpenAPI drift 复扫，并补齐 422 validation error envelope 统一验证。
