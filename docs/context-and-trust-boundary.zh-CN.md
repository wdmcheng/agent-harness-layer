# Context 与信任边界

[English](context-and-trust-boundary.md) | [简体中文](context-and-trust-boundary.zh-CN.md)

适用读者：编排 agent loop、检索、工具、审批和事件回传的 app developer；维护 trust DTO、guardrail、policy 和 runtime seam 的 scaffold maintainer。

导航：[根 README](../README.zh-CN.md) · [架构图](architecture/README.zh-CN.md) · [Adapter 合同](adapter-contracts.zh-CN.md) · [安全策略](security-policy.zh-CN.md) · [Eval/Observability](eval-observability-loop.zh-CN.md)

## 当前链路

```text
HTTP/CLI input（不可信）
  -> auth identity + permission
  -> InputGuardrail / PolicyEngine
  -> ContextInput + SourceRef/ContextRef
  -> retrieval/tool/history fragments
  -> ContextAssembler（预算、优先级、截断、trace）
  -> model/tool invocation
  -> output guard / policy / HITL
  -> CanonicalEvent + audit + checkpoint
  -> authorized EventReader
  -> CLI JSON stream 或 HTTP SSE
```

service profile 中，API 负责认证、校验和 enqueue；runtime worker 从 PostgreSQL 恢复 execution context 并执行 loop。Redis message 只携带稳定 refs。local profile 可以同进程运行，但信任语义不因此放宽。

## 信任对象与不变量

- `SourceRef` 标识信息来源；`ContextRef` 把来源、信任级别和逻辑 evidence 关联起来。
- `TrustLevel` 是封闭枚举，不允许用自由文本制造更高信任；外部输入、retrieval、tool output 必须按实际来源标级。
- `ContextInput`/`ContextOutput` 是跨层 DTO；不得夹带 raw provider object、ORM model、credential 或宿主绝对路径。
- `ContextAssembler` 对 fragments 做确定性排序、token budget、history 丢弃或 retrieval/tool 截断，并输出 `ContextFragmentTrace`/`ContextAssemblyResult`。被截断或丢弃的信息必须留决策 trace，不能静默消失。
- tenant、agent、run、request、trace 与适用 parent/delegation refs 必须随 durable evidence 保留；跨 API/worker、approval resume 和 SSE resume 不能换身份。

## 不可信输入处理

1. HTTP/CLI body 只提供业务输入；tenant、reviewer、permission 和服务身份由可信入口注入。
2. 在 retrieval、model、tool、filesystem 或 shell 副作用前执行 schema、input guardrail、permission、policy、workspace 和 budget/capacity 检查。
3. 检索文档、网页、MCP/tool output 和历史消息都可能包含 prompt injection。它们是带 source/trust refs 的数据，不是系统指令，不能覆盖 policy、tool allowlist 或 approval requirement。
4. 输出进入 event/telemetry/API 前执行 secret redaction、大小/有限数检查和可见性分类；超大 evidence 使用受控 artifact ref，而不是截断后冒充完整内容。
5. 任何无法证明来源、tenant 或信任级别的输入 fail closed，不通过“默认 trusted”恢复服务。

## Guardrail、Policy 与 HITL 回边

Input guardrail 处理输入形态与显式拒绝；`PolicyEngine` 根据 identity、permission、agent/tool/action 和配置返回 allow、deny 或 require-approval。require-approval 必须持久化 approval、暂停 run，并在批准后从 checkpoint/continuation 恢复；拒绝产生终态证据但不创建 continuation。

批准不是永久授权。`ApprovedToolExecutor` 在副作用前重新校验 grant、参数 hash、tenant/run/tool、lease 与执行状态；失去 lease 或结果不确定时进入可恢复/人工复核边界，不能重新执行“碰碰运气”。完整权限和 secret 规则见[安全策略](security-policy.zh-CN.md)。

## 事件回传

- 当前已实现：授权 `EventReader`、CLI `events stream --after-seq` 和 HTTP `GET /api/v1/runs/{run_id}/events/stream` SSE。
- cursor 语义：CLI `--after-seq` 和 HTTP `Last-Event-ID` 都是 exclusive resume；terminal 后 EOF。
- visibility：默认只读 public event；internal event 必须经同一授权策略。跨 tenant 与不存在统一收敛，避免枚举。
- 读取是零副作用：resume、断线和慢客户端不能创建 event、修改 run 或预取无界页面。
- WebSocket 是 P1 未来能力，当前没有 endpoint、协议或部署入口；不能把 SSE 客户端写成“WebSocket 已支持”。

## 验证与证据

```bash
make test
make smoke-local
# 真实 API/worker/PostgreSQL/Redis、approval continuation 与 SSE resume：
make smoke-service
```

关键证据：`tests/contracts/test_auth_policy_hitl_policy_contracts.py`、`tests/contracts/test_auth_policy_hitl_event_contracts.py`、`tests/contracts/test_retrieval_rag_contracts.py`、`tests/contracts/test_sse_authorized_reader_contracts.py`、`tests/contracts/test_sse_event_reader_postgresql_contracts.py`、`templates/service-app/scripts/service_approval_smoke.py`。

常见故障：context 与预期不符时检查 assembly trace 的排序、budget 和 drop/truncate 决策；approval 后不恢复时检查 durable resolution、queue 补投和 worker owner；SSE 401/403/404 时检查 token/permission/tenant，不要绕过 reader；重复或缺失 event 时检查 stable event id、sequence reservation 和 terminal invariant。
