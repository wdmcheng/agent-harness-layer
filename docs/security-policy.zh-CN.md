# 安全策略与故障处置

[English](security-policy.md) | [简体中文](security-policy.zh-CN.md)

适用读者：配置 service-app 安全边界的 app developer，以及维护 auth、policy、approval、workspace、secret 和 audit seam 的 scaffold maintainer。

导航：[根 README](../README.zh-CN.md) · [Context 与信任边界](context-and-trust-boundary.zh-CN.md) · [Adapter 合同](adapter-contracts.zh-CN.md) · [架构边界](architecture/README.zh-CN.md) · [Release 边界](release-process.zh-CN.md)

## 身份、认证与权限

- local profile 使用显式本地身份配置，目的只是离线开发；不能把它描述成生产认证。
- service profile 使用 HTTP Bearer/API key 验证。明文 token 只在请求入口存在，数据库保存 hash；tenant、subject、permissions 由 `TokenVerifier` 解析，body 不得覆盖。
- endpoint 先验证身份和 permission，再访问资源。跨 tenant 与不存在资源采用一致外部语义，防止 ID 枚举。
- delegation 必须经 `AgentRegistry`、identity/permission、policy、cycle/depth/budget 和 tenant 边界；不能直接调用 child executor。

## Policy 与 approval

`PolicyEngine` 的稳定结果为 allow、deny、require-approval。危险 tool/model/delegation 操作在副作用前检查 policy。require-approval 持久化请求和安全摘要，暂停 run；approve/deny 使用服务端 reviewer identity，并留下 audit。

- deny 不创建 continuation。
- approve 只授权匹配 tenant/run/tool/action/arguments hash 的一次受控执行；执行前重新校验 grant 和 lease。
- approve enqueue 失败保留可补投状态；重试只补投 continuation，不重放 handler。
- `needs_review` 或 uncertain outcome 需要人工核对 durable evidence，不自动重跑。

## Workspace 与工具

- `WorkspacePolicy` 对可读/可写 root、规范化路径和 symlink 逃逸 fail closed。
- file/shell tool 不继承完整宿主环境；只传 allowlist 环境变量，参数中的路径也必须落在 workspace。
- 工具必须在 registry 中声明 schema、权限与 policy action。不存在“临时直接执行一下”的旁路。
- tool output 经过 `guarded_tool_payload`、secret pattern 检测、大小门禁与 artifact 化后才能进入 event/model context。

## Secret 装载与脱敏

- 不提交 `.env`、token、DSN password、provider key、临时 credential 或生成状态；模板 `.gitignore` 与 `.env.example` 分别承担阻止提交和字段示例职责。
- service profile 的敏感配置使用受信 secret root 下的只读普通文件；拒绝 symlink、目录、超出 64 KiB、direct/file 同时配置与空/无效内容。
- config、doctor、health、日志、API error、event、telemetry、eval evidence 和 artifact 都必须走统一 redaction；原始 Pydantic 错误链和 traceback locals 不能外泄。
- provider raw response 不进入公共错误。脱敏后仍超大或包含危险结构时，只保留安全摘要和逻辑 evidence ref。

## Event 可见性与审计

`CanonicalEvent` 是运行证据，不等于所有调用方可见。默认 reader 只返回 public visibility；internal evidence 需要额外授权。每条相关证据保留 tenant、agent、run、request、trace 和 action/policy/approval refs。audit 记录决定和 actor，不记录 secret；provider fan-out 失败不能删除本地 audit/event。

当前没有 event retention/TTL job。未来引入清理必须单独定义 cursor 过期、合规保留和删除审计，不能在运维脚本中静默删除。

## 安全验证

```bash
make quality
make test
make smoke-local
# 需要 Docker Compose；验证真实 auth、secret file、Redis/PostgreSQL、审批恢复和日志脱敏：
make smoke-service
make license-check
```

关键证据：`tests/contracts/test_auth_policy_hitl_openapi_contracts.py`、`tests/contracts/test_auth_policy_hitl_policy_contracts.py`、`tests/contracts/test_approval_resolution_forgery_contracts.py`、`tests/contracts/test_tool_registry_authorization_contracts.py`、`tests/contracts/test_sse_authorized_reader_contracts.py`、`tests/contracts/test_observability_local_first_fanout_contracts.py`。

## 故障处置

1. 先停止受影响入口或 provider fan-out，保留 PostgreSQL/event/audit/claim evidence；不要清库或盲目重放。
2. 按 tenant/run/request/trace 关联 auth、policy、approval、tool/model usage 与 terminal event，确认是否有未决 lease 或 uncertain side effect。
3. 若疑似 secret 泄漏，轮换 credential，检查 health/doctor/log/API/event/artifact/provider payload；不要把原值复制进 issue 或测试 fixture。
4. workspace 拒绝优先核对规范化路径和 symlink；权限拒绝核对服务端 identity/permission；approval 卡住核对 resolution、queue 补投和 worker owner。
5. 修复后运行定向合同，再跑 `make test`；涉及跨进程、安全配置或 durable recovery 时必须跑真实 `make smoke-service`。

安全策略变更属于行为变更：先更新 Product Spec/API Contract 或对应 OpenSpec change，再实施与复审。
