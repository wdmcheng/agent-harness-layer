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
| `Accept` | No | 默认 `application/json`；未来 SSE endpoint 使用 `text/event-stream`。 |
| `Content-Type` | Conditional | JSON mutating request 使用 `application/json`。 |
| `X-Request-Id` | No | 调用方可传；服务端没有收到时生成 UUID，并写入响应 body 的 `request_id`。 |
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
- 当前 tests 已覆盖 404/500 走 `ApiErrorEnvelope`；422 validation error 的 envelope 统一属于后续 API 完整化检查项。

### 4.6 通用状态码

| 状态码 | 用途 |
|---:|---|
| 200 | 同步读取或同步操作成功；当前 run create/cancel/resume 也返回 200。 |
| 201 | 未来同步创建资源成功时可用；使用前必须更新本契约。 |
| 202 | 未来异步排队成功时可用；必须返回 task/run id。 |
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
| `budget.max_tokens_per_run` | integer | Yes | 单 run token 预算。 |
| `budget.max_cost_usd_per_run` | number \| null | Yes | 单 run 成本预算；`null` 表示未设置成本上限。 |
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
| `waiting` | 等待人工审批。 |
| `approved` | 已批准，runtime 可按关联 checkpoint resume。 |
| `denied` | 已拒绝，动作不得执行，run 按策略 failed 或 fallback。 |
| `cancelled` | 审批被系统或用户取消。 |

### 5.15 `ApprovalRecord`

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
| `trace_id` | string | No | 关联 trace。 |
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

Phase 8 CLI/runtime/module seam 使用的工具调用 DTO；当前不暴露为 HTTP request body。

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

Phase 8 工具调用稳定结果 DTO；当前不作为 HTTP response body 直接暴露。

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

Phase 8 工具 seam 必须使用稳定错误码，CLI、runtime 和未来 API route 都按这些 code 分支，不解析人类文案。

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
| 认证 | 已接入 `IdentityContext` dependency；local/dev 未配置 verifier 时注入默认身份，service/API key profile 要求 `Authorization: Bearer <token>` 或等价 API key；创建前必须通过 `run.create` policy check。 |
| 请求头 | `Content-Type: application/json`；可选 `Accept: application/json`、`X-Request-Id`；认证 profile 启用 verifier 时必填 `Authorization: Bearer <token>` 或等价 API key。 |
| Path 参数 | `agent_id: string`，稳定 agent ID。Agent Registry 能力落地后必须由 `AgentRegistry` 校验存在性和重复性。 |
| URL 参数 | none |
| 请求体 | `AgentRunCreateRequest` |
| 幂等性 | body 含 `idempotency_key` 时，同一 tenant/agent/session 下重复提交返回同一 run；缺失时非幂等。 |
| 副作用 | 写 run state、checkpoint/events；可能触发 model/tool/policy/worker 后续动作。当前 fake run 可同步完成或进入 waiting。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`；不保证 `X-Request-Id` response header。 |
| 响应体 | `RunCreateResponse` |
| 错误响应码 | `400 api.http_error`、`401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied` / `guardrail.denied`、`404 registry.agent_not_found` / `api.not_found`、`409 run.invalid_transition`、`422 validation_error` / `registry.invalid_config`、`500 api.internal_error`。 |
| 状态语义 | `completed/failed/cancelled` 表示 terminal；`waiting` 表示调用方需要 approval 或 resume；`running/created` 表示后续通过 events/detail 追踪。 |
| 安全规则 | API route 不得直接操作 ORM session、DBOS API 或 provider SDK；input 进入 runtime 前必须经过 `run.create` policy check 和 guardrail/trust 标注；无效 token 或缺少 `run.create` 权限不得创建 run。 |
| 验证要求 | `tests/contracts/test_runtime_checkpoint_runs_contracts.py` 必须检查 route table、OpenAPI path、helper 使用 `RunOrchestrator`、idempotency、request_id 和 error envelope；认证/策略/HITL contract tests 必须覆盖无效 token 和缺少 `run.create` 权限均不创建 run、guardrail deny 不创建半截 run、guardrail require_approval 进入 approval/checkpoint 等待。 |

### RUN-002 读取 run detail

| 字段 | 内容 |
|---|---|
| Contract ID | `RUN-002` |
| 状态 | 已实现 |
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
| 响应体 | `RunCreateResponse` |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`500 api.internal_error`。 |
| 状态语义 | 调用方根据 `status` 和 `terminal_event` 判断继续轮询、读取 events、resume、cancel 或展示终态。 |
| 安全规则 | 非当前 tenant 或不可见 run 必须返回 `404` 或 `403`，不能泄漏其他 tenant 的 run 是否存在。 |
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
| 认证 | 已接入 `IdentityContext` dependency；按 tenant/identity/event visibility 检查，`include_internal=true` 需要 policy 权限。 |
| 请求头 | 可选 `Accept: application/json`、`X-Request-Id`；认证 profile 启用 verifier 时必填 `Authorization`。 |
| Path 参数 | `run_id: string` |
| URL 参数 | `after_seq: integer >= 0`，默认 `0`；`include_internal: boolean`，默认 `false`。 |
| 请求体 | none |
| 幂等性 | 幂等读取；同一 `after_seq` 可重复读取同一事件窗口。 |
| 副作用 | none。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。未来 SSE route 必须使用 `text/event-stream`，不能复用本 JSON route 伪装成 SSE。 |
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
| 状态 | 已实现 checkpoint resume seam；完整 HITL approval resume 依赖后续认证/审批能力。 |
| 入口 / 调用方 | OpenAPI 调用方、HITL approval flow、future worker/API gateway。 |
| 用途 | 使用 resume token 恢复 checkpointed run。 |
| 方法 | `POST` |
| 路径 | `/api/v1/runs/{run_id}/resume` |
| 认证 | 已接入 `IdentityContext` dependency；认证 profile 启用 verifier 时需要有效 Bearer/API key，resume token 还必须属于 path run。 |
| 请求头 | `Content-Type: application/json`；可选 `Accept: application/json`、`X-Request-Id`；认证 profile 启用 verifier 时必填 `Authorization`。 |
| Path 参数 | `run_id: string` |
| URL 参数 | none |
| 请求体 | `RunResumeRequest` |
| 幂等性 | 非幂等；token 已消费或 run 已 terminal 时不得推进状态。 |
| 副作用 | 解析 resume token、推进 run state、写后续 events；可能触发 worker/model/tool 后续动作。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `RunCreateResponse` |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`409 run.invalid_transition`、`422 validation_error`、`500 api.internal_error`。 |
| 状态语义 | 成功后返回新的 run status；如果完成则返回 terminal event。 |
| 安全规则 | `resume_token` 必须属于 path 中的 `run_id`；错误 URL 不得推进其他 run；token 还必须匹配 tenant/identity/approval context。 |
| 验证要求 | contract tests 必须覆盖 token/run_id mismatch 先失败且不推进任一 run。 |

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

单项读取使用 `GET /api/v1/runs/{run_id}/approvals/{approval_id}`，返回 `ApprovalRecord`，写入 approval read audit evidence，并复用同一认证、可见性、脱敏和 `ApiErrorEnvelope` 规则。

### APR-002 resolve approval

| 字段 | 内容 |
|---|---|
| Contract ID | `APR-002` |
| 状态 | 已实现。 |
| 入口 / 调用方 | OpenAPI 调用方、HITL approval flow、CLI 等价入口 `agent-harness approvals approve <approval_id>` / `agent-harness approvals deny <approval_id>`、future Access/API gateway。 |
| 用途 | 对 waiting approval 执行 approve 或 deny，并按策略 resume / fail / fallback run。 |
| 方法 | `POST` |
| 路径 | `/api/v1/runs/{run_id}/approvals/{approval_id}` |
| 认证 | 必须注入 `IdentityContext`；当前身份必须有审批权限。 |
| 请求头 | `Content-Type: application/json`；可选 `Accept: application/json`、`X-Request-Id`；认证启用时必填 `Authorization`。 |
| Path 参数 | `run_id: string`、`approval_id: string` |
| URL 参数 | none |
| 请求体 | `ApprovalResolveRequest` |
| 幂等性 | 非幂等；已 resolved approval 再次 resolve 必须返回 409，不得重复推进 run 或 audit。 |
| 副作用 | 更新 approval status、写 audit log、发布 `approval.resolved` event；approve 可通过 runtime resume seam 推进 run，deny 可让 run failed 或 fallback。 |
| 成功响应码 | `200` |
| 响应头 | 当前只保证 `Content-Type: application/json`。 |
| 响应体 | `ApprovalResolveResponse` |
| 错误响应码 | `401 auth.invalid_token` / `auth.missing_credentials`、`403 policy.denied`、`404 api.not_found`、`409 approval.invalid_transition` / `run.invalid_transition`、`422 validation_error`、`500 api.internal_error`。 |
| 状态语义 | `approved` 表示原动作允许继续；`denied` 表示原动作不得执行。返回的 `run.status` 是 resolve 后 runtime 摘要。 |
| 安全规则 | path 中的 `run_id` 必须与 approval 归属一致；错误 URL 不得推进其他 run。响应和 audit 不得泄漏 resume token、secret 或原始危险 payload。 |
| 验证要求 | 认证/策略/HITL contract tests 必须覆盖 approve、deny、重复 resolve 409、跨 run resolve 拒绝、audit evidence、request_id 和 OpenAPI schema。 |

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

Phase 8 不新增 HTTP route。工具执行先通过 CLI、runtime module seam 和未来 worker seam 暴露，避免在 tool 安全边界未稳定前公开远程执行 API。后续若新增 `/api/v1/tools` 或等价 route，必须先按第 3 节补完整 endpoint 条目和 OpenAPI drift tests。

### 9.1 当前入口

| Contract ID | 状态 | 入口 / 调用方 | 用途 |
|---|---|---|---|
| `TLS-001` | Phase 8 目标 | `agent-harness tools list`、runtime registry seam | 列出当前 actor/agent 可见的内置工具、FileTool、ShellTool 和 MCP discovery 工具摘要。 |
| `TLS-002` | Phase 8 目标 | `agent-harness tools call`、runtime registry seam | 通过 `ToolRegistry` 执行一次受 policy 控制的工具调用，输出 `ToolCallResult`。 |
| `TLS-003` | Phase 8 目标 | `agent_harness.tools.ToolRegistry` | 供 runtime、worker 和 template agent 通过 module seam 调用工具，不暴露 callable 或 vendor SDK object。 |

### 9.2 行为契约

| 字段 | 约束 |
|---|---|
| 认证 / 身份 | CLI 使用 profile 中的 `IdentityContext`；runtime/worker 必须传入已认证 actor。所有 mutating、shell、MCP、workspace 外访问和危险动作都进入 `PolicyEngine`。 |
| 请求 DTO | `ToolCallRequest`；CLI arguments 可来自 JSON 字符串或文件，但进入 registry 前必须转换成该 DTO 形状。 |
| 响应 DTO | `ToolCallResult`；CLI 输出可用文本或 JSON，但字段语义不得偏离 DTO。 |
| 幂等性 | 工具执行默认不幂等；调用方必须通过 run/trace/invocation id 关联审计。读文件和 list/search 是逻辑读操作，仍要记录 invocation evidence。 |
| 副作用 | FileTool 可读写 workspace；ShellTool 可启动受控子进程；MCP client 可连接配置 server。所有副作用必须先通过 schema validation、allowlist 和 policy。 |
| 持久化 | Phase 8 必须持久化 `workspaces` 和 `tool_invocations`，至少记录 workspace root/policy ref、tool name、args_ref、result_ref、status、duration、tenant/run/agent/trace。大参数和结果走 artifact/ref。 |
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

## 10. 保留 API 索引

这些路径来自 `Product-Spec.md` 的当前版本 API 列表。它们不是当前已实现能力；对应计划项开工前必须先把本节扩展成第 3 节规定的完整 endpoint 条目，再写 route。

| Contract ID | 状态 | 计划归属 | 路径 | 契约门禁 |
|---|---|---:|---|---|
| `EVL-001` | 规划中 | Eval Gate | `/api/v1/eval-cases/drafts` | 必须定义 draft list/create/review schema、secret scan、trace source、不可自动进入 approved。 |
| `EVL-002` | 规划中 | Eval Gate | `/api/v1/eval-cases/approved` | 必须定义 approved dataset 写入权限、人工确认、audit 和回滚/归档规则。 |
| `EVL-003` | 规划中 | Eval Gate | `/api/v1/evals/runs` | 必须定义 eval run create/detail/list schema、score sink、provider failure 降级。 |
| `HLT-001` | 规划中 | Service App / Service Profile | `/api/v1/health` | 必须定义 local/service profile health 字段、storage/queue/observability 状态和公开性。 |

## 11. 入口 / 调用方映射

| 入口 / 调用方 | 当前或目标接口 | 说明 |
|---|---|---|
| `agent-harness run <agent_id>` | 等价于 `RUN-001` 的 runtime seam | CLI 不走 HTTP，但必须使用同一 `RunOrchestrator`、storage、event bus 和 DTO 语义。 |
| `agent-harness agents list` | 等价于 `AGT-001` 的 registry seam | CLI 不走 HTTP，但必须使用同一 `AgentRegistry`、descriptor DTO、identity/policy visibility 和 validation 语义。 |
| `agent-harness policy check` | 等价于 `POL-001` 的 policy seam | CLI 不走 HTTP，但必须使用同一 `PolicyEngine`、identity、audit 和 decision DTO。 |
| `agent-harness approvals list/approve/deny` | 等价于 `APR-001` / `APR-002` 的 approval seam | CLI 不走 HTTP，但必须使用同一 `ApprovalService`、runtime resume 和 audit seam。 |
| `agent-harness tools list/call` | 等价于 `TLS-001` / `TLS-002` 的 tool execution seam | CLI 不走 HTTP，但必须使用同一 `ToolRegistry`、PolicyEngine、workspace guard、artifact store、audit 和 DTO 语义。 |
| runtime / worker tool call | 等价于 `TLS-003` 的 module seam | runtime/worker 必须通过 `ToolRegistry`，不得直接调用 FileTool、ShellTool、MCP SDK、subprocess 或文件系统危险操作。 |
| OpenAPI 调用方 | `AGT-001`、`RUN-001` 到 `RUN-005`，后续保留 API | `/docs`、`/redoc`、`/openapi.json` 是当前版本管理面，不是前端 SaaS UI。 |
| service-app FastAPI | `AGT-001`、`RUN-001` 到 `RUN-005` | route module 保持薄层，app factory 负责依赖注入、lifecycle 和 error handler。 |
| runtime worker | 内部 worker seam；不直接新增 HTTP route | worker 必须通过 runtime components，不直接操作 ORM/DBOS/provider SDK。 |
| HITL approval flow | `RUN-005` + `APR-*` | approval/resume 必须关联 checkpoint、audit、tenant、run、identity。 |
| Eval review flow | `EVL-*` | draft 到 approved 必须人工确认，secret/隐私脱敏是写入门禁。 |
| future API/worker split | 所有 HTTP API + worker seam | 拆分后数据只走 DTO、CanonicalEvent、repository/provider/facade，不传进程内可变对象；queue message header 必须携带 `request_id` 和 `idempotency_key`。 |

## 12. 流式与事件契约

当前实现：

- `GET /api/v1/runs/{run_id}/events` 返回 JSON `RunEventsResponse`。
- 该 route 按 `after_seq` 读取 `CanonicalEvent`，不是 SSE 握手 endpoint。

未来 SSE/WS adapter：

- SSE/WS 是 Access 层输出协议，不能替代内部 `CanonicalEvent` 模型。
- SSE event 必须由 `CanonicalEvent` 显式映射，保留 `seq`、`event_type`、`terminal`、`visibility` 和适用的 `trace_id/request_id`。
- SSE adapter 必须把客户端 `Last-Event-ID` 映射为 `CanonicalEvent.seq` 续读起点；JSON events seam 继续使用 `after_seq`。
- 断线恢复必须以 `seq` 为准；final 结算以 terminal event 为准。
- 握手前错误走 `ApiErrorEnvelope`；握手后错误必须转成可序列化 event，且不得泄露 secret/provider 原始错误。

## 13. OpenAPI 生成与漂移检查

当前必须保留：

- FastAPI app 暴露 `/openapi.json`、`/docs`、`/redoc`。
- OpenAPI paths 必须包含当前已实现 run routes。
- `RunCreateResponse` schema 必须包含 `request_id`。
- `RunCreateResponse` schema 不得包含 `resume_token`。
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

新增 `approvals` 和 `policies` route 时，应按认证、策略、HITL 能力边界拆出对应 contract tests，不把所有 OpenAPI 检查堆进一个大测试。`agents` route 使用 `tests/contracts/test_agent_registry_model_context_contracts.py` 单独覆盖，认证/策略/HITL 还要补 401/403 和可见性检查。

## 14. 契约验收清单

- [x] 已区分当前已实现 run API 与保留 API。
- [x] 已按架构图映射 Access、Runtime、Engine、Tools、Infra、Eval Gate、Observability 和部署拆分边界。
- [x] 已固定当前 run API 的 method、path、request、response、错误 envelope、幂等性、副作用和安全规则。
- [x] 已明确 events JSON seam 与未来 SSE/WS adapter 的边界。
- [x] 已明确 `reasoning.delta` 默认不可见。
- [x] 已明确 API route 不得暴露 ORM、DBOS、provider SDK 或进程内 handle。
- [x] 已明确新增/修改 endpoint 必须先改本契约，再做局部 OpenAPI drift 检查。
- [x] Agent Registry 开工前补全 `AGT-001` 的完整 endpoint 条目和 contract tests。
- [x] Auth / Policy / HITL 开工前补全 auth、policy、approval endpoint 条目和 contract tests 目标。
- [x] Tool execution 开工前补全 tools CLI/runtime/module seam、无新增 HTTP route 和 contract tests 目标。
- [ ] Eval Gate 开工前补全 eval endpoint 条目和 contract tests。
- [ ] Service App / Service Profile 收口时做全量 OpenAPI drift 复扫，并补齐 422 validation error envelope 统一验证。
