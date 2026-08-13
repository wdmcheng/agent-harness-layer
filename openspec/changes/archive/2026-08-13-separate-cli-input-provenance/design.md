## Context

CLI 目前通过修改业务 input 表示 transport 来源。这违反 Access 与 Runtime 的边界：业务 DTO 属于 Agent 输入，来源属于受信 execution context。仓库已有私有 execution-context JSON、repository/UoW 与 run recovery seam，修复应在这些既有所有权内增加一个封闭 DTO/classifier，而不是扩展公开 schema或 queue protocol。

approval resume 的真实缺陷是 correlation owner 选择错误。恢复路径已经能读取 durable private context，但重建 executor/continuation context 时必须把 classifier 返回的 request id 当作 authoritative execution 值。当前 approval/resume 入口的 request id 代表本次 resolution transport，并继续用于 APR-002 operation、`run.resumed` 与新 terminal event；它不能覆盖原 execution context，原值也不能覆盖当前入口 correlation。

## Goals / Non-Goals

**Goals**

- 业务 input 只保留调用方字段，可信 CLI 来源独立传播。
- 用一个封闭 typed provenance 和一个 private classifier 处理必要 create/replay/recovery 路径。
- 无损保存 nullable execution request id，并让 approval resume 的 executor 使用 classified authoritative 值，同时保持新 resume/terminal event 的当前入口 request id。
- 保持公开 schema、provider request、delegation 与既有 queue 协议不变。

**Non-Goals**

- 不重新设计 queue claim、worker ownership、DBOS 或 durable recovery 协议。
- 不引入 `ClaimableRunQueue`、Redis inspect/fenced claim、row-lock claim 或兼容/回滚平台。
- 不建立大规模 hostile matrix、Docker/Registry/FD evidence 或 production validation service。
- 不新增任意 metadata bag、公开 provenance 参数、数据库列或 migration。

## Decisions

### Decision 1: 使用封闭 typed provenance，而不是特殊业务字段

在 `runtime/_continuation_context.py` 定义只允许 `source=cli` 的不可扩展 DTO。CLI composition 显式构造该值，并通过 `RunOrchestrator` 的私有 submission seam 传递；公开 `RunOrchestrator.start_run` 的参数集合不变，`agent_harness.runtime` 不导出该 DTO。runtime 不从 input 中 pop/filter `source`，因此业务 schema 可以合法拥有同名字段，也不能由普通公开 caller 伪造 CLI provenance。

### Decision 2: exact private envelope 由唯一 classifier 解释

既有 private execution-context JSON 继续保存 `identity`、顶层 nullable `request_id`、`trace_id` 与 `checkpoint_state` 等既有字段；本 change 只允许新增可选键 `input_provenance`。CLI provenance 的值必须是 exact object：

```json
{
  "schema_version": "run-input-provenance-v1",
  "source": "cli",
  "execution_request_id": null
}
```

字段集合必须恰为 `schema_version/source/execution_request_id`；前两项只能取上述常量，`execution_request_id` 只能是非空字符串或 JSON `null`，并必须与同一 execution-context 顶层 `request_id` 逐值相同。内部 `RunInputProvenance` DTO 只承载封闭的 `source=cli`；envelope 把该 typed 来源与恢复所需的 authoritative nullable execution request id 绑定。缺少 `input_provenance` 是合法 legacy/非 CLI context，classifier 仍从既有顶层 `request_id` 恢复 authoritative nullable 值但返回无 CLI provenance。

旧键 `provenance`、未知版本/来源、额外或缺失字段、错误类型、空字符串 ID，以及 envelope 与顶层 `request_id` 不一致，统一返回 `execution_context.provenance_invalid`。queue message 的非空 `request_id` 只是 delivery correlation，APR-002 lease 的 `resolution_request_id` 只是当前 approval/resume correlation；二者都不得填充或覆盖 `execution_request_id`。caller 不直接解析 JSON，不从 queue 或当前环境猜测。repository 只提供读取该私有记录所需的窄 seam，不扩大公开 `RunRecord`。

### Decision 3: 必要路径显式携带分类结果

首次 create 负责写入；幂等 replay、terminal recovery、queued rebuild 与 approval resume 负责读取。适用的 guardrail、audit、executor/continuation 接收 typed 结果。普通非 CLI input、公开 API 与 delegation 不需要新参数。

### Decision 4: 恢复路径明确区分 execution 与当前入口 request-id 平面

classifier 返回的 nullable `execution_request_id` 是原 execution 的 authoritative correlation。approval resume 必须把该值交给重建的 executor/continuation context；`None` 是有效权威值，不自动生成替代 ID。`approvals/_continuation.py` 从既有 `ApprovalResolutionLease.resolution_request_id` 取得当前 resume request id，并通过私有 continuation seam 交给 `runtime/_run_continuation.py`；公开 `RunOrchestrator.resume_run` 参数集合保持不变。APR-002 resolution operation以及本次新发布的 `run.resumed`/terminal event 使用这个当前 resolution request id。

service queued terminal recovery 同样保留双平面：executor/rebuild 继续使用 classified authoritative execution 值；DBOS 确定性失败或终态 evidence 中断后的新 terminal event 使用当前 queue message 的非空 delivery `request_id`。worker 只把这个既有字段传给 orchestrator 的私有失败收口 seam，不把它写回 execution context，也不改变 queue DTO、pickup/reclaim、claim、ack 或 DBOS 语义。实现和测试必须覆盖 execution 值为 `None` 的情况，并逐值断言两个平面互不覆盖。

### Decision 5: queue delivery 流程保持原样

worker 仍先通过既有 pickup/reclaim 取得 run reference，再从 PostgreSQL durable run 读取 private context 并分类。该顺序足以修复来源污染与恢复值错误；它不声称解决 queue ownership 前分类，也不引入新的 queue capability。

## Affected Surfaces / 文件所有权

本 change 的实现独占与串行共享范围必须与 change matrix 保持逐路径一致：

- 生产独占：`packages/agent-harness/src/agent_harness/cli.py`、`packages/agent-harness/src/agent_harness/policy/engine.py`、`packages/agent-harness/src/agent_harness/approvals/_continuation.py`、`packages/agent-harness/src/agent_harness/approvals/_queue_resolution.py`、`packages/agent-harness/src/agent_harness/runtime/executor.py`、新建 `packages/agent-harness/src/agent_harness/runtime/_continuation_context.py`、`packages/agent-harness/src/agent_harness/runtime/_run_lifecycle.py`、`packages/agent-harness/src/agent_harness/runtime/_queued_run_orchestration.py`、`packages/agent-harness/src/agent_harness/runtime/_run_continuation.py`、`packages/agent-harness/src/agent_harness/storage/run_repositories.py`、`packages/agent-harness/src/agent_harness/storage/repositories.py`；`approvals/_queue_resolution.py` 只在既有DBOS确定失败收口中把resolution state的当前 request id交给现有私有terminal evidence恢复seam；四个模板适配层 `agents/examples/dev_assistant/agent.py`、`rag_assistant/agent.py`、`repo_analyst/agent.py`、`ticket_triage/agent.py` 只删除既有 `payload.pop("source", None)` 特判并保留其他业务归一化。
- 串行共享验收：`templates/service-app/app/workers/runtime_worker.py`。本 change 不得改 harden 已验收的 profile/profiles-dir/once/env-file hunk或既有业务 input；只允许在 DBOS 确定性失败分支把 `message.request_id` 转发给既有 orchestrator 私有 queued terminal recovery seam，并验证 pickup/reclaim → `execute_run` 从 durable run 进入 classifier。不得增加 private context 参数、queue DTO、claim、ack 或 DBOS 协议。完成后必须重跑两个 change 的聚焦合同。
- 明确不修改 `packages/agent-harness/src/agent_harness/runtime/__init__.py`，不新增公共 export，也不改变公开 `RunOrchestrator.start_run` 或 `resume_run` 的参数集合。
- 测试：`tests/contracts/test_cli_input_provenance_contracts.py`、`tests/contracts/test_cli_input_provenance_recovery_contracts.py`。
- change artifact、`API-Contract.md` 与 living plan/matrix 由主 Agent 串行维护。唯一例外是三张契约票通过、仓库外快照验证完成后，主 Agent 按 change matrix `joint-crop-v1` 的逐路径 manifest 恢复/删除已放弃膨胀；该事务不改变任何 HEAD queue/runtime/运维行为。除此之外，除非先修约并重审，不得修改其他生产、测试、脚本、配置、Compose、queue adapter、migration 或运维文件。

`joint-crop-v1` 的 owner、51个 tracked 恢复路径、39个 untracked 删除路径与21个逐 hunk 保留路径完整写在 change matrix 5.6.1；本 design 逐值采用该版本，不允许 glob、目录级删除或清单外恢复。`approvals/_continuation.py` 在预裁剪时为 clean，不是 dirty manifest 输入，裁剪后才按上述独占 owner 修改；`approvals/_queue_resolution.py` 是51个 tracked 恢复路径之一，裁剪时先恢复为 HEAD，随后才按2.5的独占 owner 写入当前 resolution request id 的直接因果 hunk。发现任一无关用户 hunk时必须保留该 hunk并停止对应整项恢复。

## Risks / Trade-offs

- **私有 JSON 兼容**：合法 legacy/非 CLI context 必须有明确分类；malformed context 失败关闭，且不回填。
- **重复解析**：所有相关 caller 必须复用唯一 classifier/repository seam，避免第二条手写 JSON 路径。
- **request id 混淆**：测试同时提供 authoritative execution 值、不同当前 resume/delivery 值与 `None`，逐值断言 executor 使用前者、APR-002/resumed/新 terminal 使用对应当前入口值。
- **范围外安全议题**：queue claim 前分类可能是独立架构问题，但与本 Bug 无直接因果关系，不在本 change 中扩张协议。

## Migration Plan

无需数据库 migration。契约三票后先验证仓库外快照，再执行 `joint-crop-v1`；随后保留真实 CLI → 严格 DTO、private context round-trip、queued rebuild 和 approval resume 的聚焦红灯，再加入 typed provenance/classifier 与必要接线。既有公开记录、queue message、provider request 和 delegation 数据无需改写。回滚不得恢复向业务 input 注入 transport `source` 的缺陷。

## Open Questions

无阻断性问题。
