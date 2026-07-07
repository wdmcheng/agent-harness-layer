---
name: api-contract-template
description: API-Contract.md 输出模板。用于有前端 UI + 后端 API 的项目，在 DEV-PLAN.md 之前固定字段级前后端接口契约。
---

# API Contract 输出模板

本模板用于生成 `API-Contract.md`。适用条件：产品存在前端 UI、后端 API、异步任务、文件上传、流式响应、或前后端需要并行开发。

原则：接口文档不是 OpenAPI 或等价运行时接口文档的事后摘要，而是开发前的字段级契约。先按需求和设计定契约，再实现 API；新增或修改 endpoint 的功能点验收当场用 `/openapi.json` 或等价运行时接口文档做局部漂移检查，最后只做全量复扫。

---

# API Contract: {{产品名称}}

> 本文件定义 {{产品名称}} 前后端接口契约。开发时先按本文档实现，再由后端框架生成 OpenAPI 或等价运行时接口文档。
> 如果本文档、`Product-Spec.md` 和运行时接口文档不一致：先按 `Product-Spec.md` 判断范围，再更新本文档，最后让实现和运行时接口文档对齐。

---

## 0. 契约目标

- 让前端按设计稿开发时不猜字段、不猜状态、不猜错误。
- 让后端实现 API 时知道每个页面需要哪些数据、异步任务、流式事件和本地化错误。
- 固定接口文档书写规范，后续所有 endpoint 必须按同一格式补充。
- 固定契约验证时机，避免把局部接口问题攒到发布前才发现。
- 明确本版本不提前引入的公共 API、多租户、复杂鉴权或非目标能力。

---

## 1. 接口文档规范

每个 HTTP endpoint 必须包含以下字段：

| 字段 | 必填 | 说明 |
|---|---:|---|
| Contract ID | Yes | 稳定编号，例如 `AUTH-001`、`SRC-001` |
| 页面 / 使用方 | Yes | 哪些页面、后台任务或外部调用方使用 |
| 用途 | Yes | 一句话说明用户或系统要完成什么 |
| 方法 | Yes | `GET` / `POST` / `PUT` / `PATCH` / `DELETE` |
| 路径 | Yes | 例如 `/resources/{resource_id}` |
| 认证 | Yes | `public`、`session required`、`api key required`、`worker internal` |
| 请求头 | Yes | 必填和可选 header |
| Path 参数 | Conditional | 路径参数名、类型、校验 |
| URL 参数 | Conditional | query 参数名、类型、默认值、分页、排序、过滤 |
| 请求体 | Conditional | JSON / multipart / none；必须引用 schema |
| 幂等性 | Yes | 是否幂等；重复请求如何处理 |
| 副作用 | Yes | 是否写库、写文件、创建 task、触发 worker、调用外部服务 |
| 成功响应码 | Yes | `200`、`201`、`202`、`204` 等 |
| 响应头 | Yes | `Content-Type`、`Cache-Control`、SSE header、文件 hash 等 |
| 响应体 | Conditional | 必须引用 schema；无 body 写 `none` |
| 错误响应码 | Yes | 该 endpoint 可能返回的业务错误 |
| 前端状态 | Yes | 该接口如何驱动 loading / empty / error / disabled / success |
| 安全规则 | Yes | secret、权限、日志脱敏、CSRF、可见性过滤、数据保护等 |

写作边界：API-Contract.md 是长期接口契约，不把 `P0/P1`、`Phase N` 等开发阶段或优先级标签铺成正文叙事；如必须引用，只作为 DEV-PLAN 条目、文件名、API path、schema 字段、注释等稳定标识出现。

流式 endpoint 还必须补充：

| 字段 | 必填 | 说明 |
|---|---:|---|
| 流协议 | Yes | SSE / WebSocket / chunked JSON / NDJSON |
| 事件顺序 | Yes | 允许出现的 event 顺序 |
| 事件 schema | Yes | 每种 event 的 JSON payload |
| 断线语义 | Yes | 前端断开、后端失败、模型失败分别怎么处理 |
| final 结算 | Yes | final event 中哪些字段为权威结果 |

文件上传 / 下载 endpoint 还必须补充：

| 字段 | 必填 | 说明 |
|---|---:|---|
| Content-Type | Yes | `multipart/form-data`、`application/octet-stream`、`text/*` 等 |
| 文件字段 | Yes | 字段名、文件名、大小上限、扩展名或 MIME |
| hash / range | Conditional | 是否返回 hash、etag、range、line range |
| 原始文件保护 | Conditional | 涉及原始证据文件时，必须声明只读、不可自动改写或改写路径 |

---

## 2. 通用约定

### 2.1 Base URL

| 环境 | Web | API |
|---|---|---|
| {{环境名}} | `{{web_url}}` | `{{api_url}}` |

### 2.2 认证与会话

- {{哪些接口 public，哪些接口需要认证。}}
- {{认证方式：session cookie / bearer token / API key / internal token。}}
- {{cookie 属性、token 过期、登出或撤销语义。}}
- {{mutating request 是否需要 CSRF header 或等价保护。}}

### 2.3 通用请求头

| Header | 必填 | 说明 |
|---|---:|---|
| `Accept` | No | 默认 `application/json`；流式接口按协议指定 |
| `Content-Type` | Conditional | JSON、multipart 或其他 |
| `X-Request-Id` | No | 调用方可传；服务端没有收到时生成 |
| `{{安全 header}}` | Conditional | {{CSRF / internal token / idempotency key}} |

### 2.4 通用响应头

| Header | 说明 |
|---|---|
| `X-Request-Id` | 每个响应都返回，用于日志排查 |
| `Cache-Control` | 默认 `no-store`，除非明确可缓存 |
| `Content-Type` | JSON、SSE、文件下载等 |

### 2.5 成功响应格式

非分页 JSON：

```json
{
  "data": {},
  "meta": {
    "request_id": "req_...",
    "served_at": "2026-01-01T00:00:00Z"
  }
}
```

分页 JSON：

```json
{
  "data": [],
  "page": {
    "limit": 50,
    "offset": 0,
    "total": 123,
    "has_more": true
  },
  "meta": {
    "request_id": "req_...",
    "served_at": "2026-01-01T00:00:00Z"
  }
}
```

异步操作 JSON：

```json
{
  "data": {
    "task": {
      "id": "task_...",
      "type": "{{task_type}}",
      "status": "queued",
      "stage": "{{stage}}",
      "created_at": "2026-01-01T00:00:00Z"
    }
  },
  "meta": {
    "request_id": "req_...",
    "served_at": "2026-01-01T00:00:00Z"
  }
}
```

### 2.6 错误响应格式

```json
{
  "error": {
    "code": "{{stable_error_code}}",
    "message": "{{可直接展示的本地化错误消息}}",
    "details": {},
    "request_id": "req_..."
  }
}
```

规则：

- `message` 必须是可直接展示的本地化文案。
- `code` 是稳定英文枚举，给前端分支处理。
- `details` 不得包含 secret、cookie、token、敏感原始响应。
- 表单校验错误使用 `422 validation_error`，字段错误放 `details.fields`。

### 2.7 通用状态码

| 状态码 | 用途 |
|---:|---|
| 200 | 同步读取或同步操作成功 |
| 201 | 同步创建资源成功 |
| 202 | 已创建异步任务，结果看任务或轮询接口 |
| 204 | 成功且无响应体 |
| 400 | 请求语义错误 |
| 401 | 未认证或会话失效 |
| 403 | 已认证但权限、CSRF 或操作限制不通过 |
| 404 | 资源不存在或当前视图不可见 |
| 409 | 状态冲突 |
| 413 | 上传体过大 |
| 415 | Content-Type 或文件类型不支持 |
| 422 | 字段校验失败 |
| 429 | 速率限制或排队上限 |
| 500 | 未预期服务端错误 |
| 503 | 外部依赖、worker、数据库或模型暂不可用 |

### 2.8 分页、排序与过滤

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `limit` | integer | 50 | 1-100 |
| `offset` | integer | 0 | 从 0 开始 |
| `sort` | string | endpoint 定义 | 稳定字段名 |
| `order` | enum | `desc` | `asc` / `desc` |

多选过滤使用逗号分隔，例如 `status=running,failed`。普通 UI 展示本地化标签，接口使用稳定 enum。

### 2.9 ID、时间与枚举

- 所有 ID 使用字符串，前端不得依赖具体格式。
- 时间使用 ISO 8601 UTC 字符串。
- 内部 enum 只出现在 API 和技术详情里，普通 UI 必须通过 label map 显示。

### 2.10 可见性与数据保护

- {{哪些资源默认不可见，哪些管理入口可见。}}
- {{删除、隐藏、归档、旧版本如何影响列表、搜索、问答或导出。}}
- {{敏感字段如何脱敏，哪些字段永不返回。}}

---

## 3. 通用 Schema

### 3.1 {{SharedSchemaName}}

```json
{
  "id": "{{id}}",
  "display_label": "{{本地化显示标签}}"
}
```

> 在这里定义跨接口复用的 schema，例如 User、TaskSummary、ErrorDetail、EvidenceItem、FileMetadata、Pagination、Confidence 等。

---

## 4. {{接口分组名称}}

### {{GROUP}}-001 {{Endpoint Name}}

| 字段 | 内容 |
|---|---|
| 页面 / 使用方 | {{页面、后台任务、外部调用方}} |
| 用途 | {{一句话说明}} |
| 方法 | `{{METHOD}}` |
| 路径 | `{{/path/{id}}}` |
| 认证 | {{public / session required / api key required / worker internal}} |
| 请求头 | {{headers 或 none}} |
| Path 参数 | {{参数表或 none}} |
| URL 参数 | {{参数表或 none}} |
| 请求体 | {{schema 名或 none}} |
| 幂等性 | {{幂等 / 非幂等；重复请求语义}} |
| 副作用 | {{none / 写库 / 写文件 / 创建 task / 调外部服务}} |
| 成功响应码 | {{200 / 201 / 202 / 204}} |
| 响应头 | {{headers}} |
| 响应体 | {{schema 名或 none}} |
| 错误响应码 | {{400, 401, 403, 404, 409, 422, 503}} |
| 前端状态 | {{loading / empty / error / disabled / success 如何表现}} |
| 安全规则 | {{secret、权限、日志脱敏、可见性、原始文件保护等}} |

`{{RequestSchema}}`:

```json
{
  "{{field}}": "{{value}}"
}
```

`{{ResponseSchema}}.data`:

```json
{
  "{{field}}": "{{value}}"
}
```

---

## 5. 流式接口契约（如适用）

### {{STREAM}}-001 {{Stream Endpoint Name}}

| 字段 | 内容 |
|---|---|
| 页面 / 使用方 | {{页面}} |
| 用途 | {{一句话说明}} |
| 方法 | `POST` |
| 路径 | `{{/stream-path}}` |
| 认证 | {{认证方式}} |
| 请求头 | `Content-Type: application/json`, `Accept: text/event-stream`, {{安全 header}} |
| 请求体 | `{{StreamRequest}}` |
| 幂等性 | 非幂等；每次请求创建一次运行记录 / 或说明幂等键 |
| 副作用 | {{写运行记录 / 调模型 / 调外部服务}} |
| 成功响应码 | 200 |
| 响应头 | `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-store`, `X-Accel-Buffering: no` |
| 响应体 | SSE events |
| 错误响应码 | 握手前：400, 401, 403, 409, 422, 503；握手后：发送 `error` event |
| 前端状态 | {{先展示什么，流式更新什么，final 如何结算}} |
| 安全规则 | {{final 前哪些内容不是权威结果；敏感信息不得进入事件}} |

SSE event 顺序：

```text
event: start
data: {"run_id":"run_...","stage":"{{stage}}","message":"{{本地化消息}}"}

event: {{ready_event}}
data: {"items":[]}

event: delta
data: {"text":"{{partial text}}","sequence":1}

event: final
data: {"run_id":"run_...","result":{}}
```

允许的事件：

| event | 必须顺序 | 说明 |
|---|---|---|
| `start` | 第一条 | 创建运行记录 |
| `{{ready_event}}` | `delta` 前必须出现，如适用 | 稳定前置结果 |
| `delta` | 0-N 条 | 流式片段 |
| `final` | 成功最后一条 | 权威结果 |
| `error` | 任意阶段 | 失败后终止流 |

断线语义：

- 前端主动断开：{{后端如何停止或标记取消。}}
- 外部服务失败：发送 `error` event，运行记录标记 failed。
- 证据不足 / 数据不足：{{是否仍发送 final，是否禁止强答。}}

---

## 6. 文件上传 / 下载契约（如适用）

### {{FILE}}-001 {{Upload Endpoint Name}}

| 字段 | 内容 |
|---|---|
| 方法 | `POST` |
| 路径 | `{{/upload}}` |
| 请求头 | `Content-Type: multipart/form-data`, {{安全 header}} |
| 请求体 | `file`: {{扩展名/MIME/大小上限}}；其他字段：{{metadata}} |
| 成功响应码 | 201 / 202 |
| 响应体 | {{resource + task 或 resource}} |
| 错误响应码 | 401, 403, 409, 413, 415, 422 |
| 安全规则 | {{原始文件保护、hash、不可自动改写、病毒扫描或隐私要求}} |

---

## 7. 页面到接口映射

| 页面 | 首屏必需接口 | 交互接口 |
|---|---|---|
| {{Screen Name}} | `GET {{endpoint}}` | `POST {{endpoint}}`, `DELETE {{endpoint}}` |

规则：

- 每个设计稿 P0 页面必须出现在本表。
- 首屏接口必须足够驱动默认、加载、空、错误、禁用和成功状态。
- 异步操作必须返回 task/run id，并能跳到任务或运行详情。

---

## 8. 运行时接口文档生成要求

- 后端应暴露 `/docs` 和 `/openapi.json`，或项目技术栈等价的运行时接口文档；产品明确不用 HTTP API 时除外。
- OpenAPI tag 或等价分组按接口分组命名。
- 每个 endpoint 的 summary 使用本地化短句。
- 每个错误 code 至少有一个 schema example。
- 运行时接口文档中不得出现 secret 明文字段。
- `API-Contract.md` 中定义的 schema 名称应尽量对应后端模型名，方便查找。
- 新增或修改 endpoint 的功能点或对应 DEV-PLAN 验收必须对照 `/openapi.json` 或等价运行时接口文档检查该 endpoint、schema、错误码、响应头和前端状态是否漂移。
- 发布前全量检查只做复扫和证据汇总，不作为第一次发现契约问题的入口。

---

## 9. 契约验收清单

- [ ] 每个页面的首屏数据接口明确。
- [ ] 每个用户操作对应 endpoint、状态码、错误码和前端状态。
- [ ] 异步操作都返回 task/run id。
- [ ] 流式接口定义了 request、headers、event 顺序、event schema 和断线语义。
- [ ] 文件上传定义了 multipart 字段、文件限制和原始文件保护。
- [ ] 所有 mutating request 定义了 CSRF、幂等键或等价保护。
- [ ] 普通 UI 需要的本地化 label 字段或 label map 边界明确。
- [ ] 可见性过滤规则明确。
- [ ] secret 脱敏规则覆盖配置、任务、日志和外部服务响应。
- [ ] 每个新增或修改 endpoint 已在对应功能点验收中完成局部 OpenAPI 或等价运行时接口文档漂移检查。
- [ ] 发布前全量接口契约漂移复扫通过，且没有把局部接口问题留到最后集中修。
