## 1. 公共 Agent execution seam

- [x] 1.1 为 runtime 定义 typed `AgentExecutor` request/context/result/approval DTO 与 protocol，并以 contract tests 固定 completed、waiting 和 failed 结果。
- [x] 1.2 扩展 registry config/private resolver，安全加载 agent package 内 executor reference；同步迁移 basic/fake agent 与全部测试 config，测试合法入口、缺失 executor、越界/缺失 module/错误对象整体拒绝，以及 public descriptor 不新增 callable/module/path。
- [x] 1.3 让 `RunOrchestrator` 在 started event 后真实调用 executor，并在 resume 时重新进入持久化 continuation；测试 typed output、唯一 terminal event、waiting checkpoint + approval record、真实 resumed output、controlled failure，且证明不存在无 executor 时固定返回 `fake-ok` 的隐式 fallback。
- [x] 1.4 更新 CLI/API composition 注入 executor resolver、assembly/model/retrieval/tool/approval/telemetry services，证明 `agent-harness run <agent_id>` 真实执行示例而非固定 `fake-ok`。

## 2. Approval continuation 与单次工具执行

- [x] 2.1 先更新 API Contract `RUN-005`、`ApprovalStatus` 和 `APR-002` 与对应 OpenAPI/API/CLI contract tests，固定普通 checkpoint 可公开 resume、approval-gated checkpoint 的公开 `RUN-005` 在消费 token/调用 handler 前返回 `409 run.invalid_transition`，以及 delayed public status、approve/deny 仲裁的 `approval.resolution_in_progress`、不确定执行的 `approval.execution_needs_review`、重复 public resolve 409 和“approved 表示允许执行、不保证动作成功”；private lease/internal state 不进入 DTO/OpenAPI。
- [x] 2.2 再新增 migration/repository seam：public approval 保持 waiting 的私有 resolution lease/state，以及 nullable unique `tool_invocations.approval_id`、arguments hash、execution state/result ref；approve/deny 通过同一条件更新原子仲裁。SQLite/PostgreSQL contract tests 证明 deny 先赢时 approve 无 lease且 handler 为零，approve lease 先赢时 deny 返回 in-progress 409 且 public status 仍 waiting，并发 approve 只有一个 lease，同一 approval 最多一个有效 resolution event/audit；并证明生产 forward-only schema 可由上一版本 repository/UoW 继续基础读写，`downgrade()` 在存在任一 resolution/claim 数据时拒绝、仅在相关字段全空时按逆序安全删除。
- [x] 2.3 定义并验证 `ApprovalGrant`，将 approval/checkpoint 的 tenant/identity/agent/run/action/resource/args hash 逐项绑定；mismatch、deny 和伪造 grant 不调用 handler。
- [x] 2.4 实现 `ApprovalService -> RunOrchestrator` 内部 resume -> `AgentExecutor.resume -> ToolRegistry.call_approved` continuation，测试审批前 0 次、approval-gated 原始 token 直接调用公开 `RUN-005` 返回 409且 handler 为 0、approve 后 1 次且 run output 来自真实结果、重复/并发 public resolve 仍为 1 次、deny 为 0；并在 run 已 waiting 后用同一持久化 storage 重建 registry、executor resolver、orchestrator 和 approval service，再通过 `APR-002` approve，证明 handler 恰好一次、真实结果、唯一 terminal，且公开 resume token 不参与执行；内部恢复读取既有 result 不改变 public 409语义。
- [x] 2.5 测试 lease 后/tool claim 前硬退出可在 owner timeout 后由真实 APR-002 重试原子换发 fencing id并恢复，未过期 lease与已有 claim不被抢占，旧 owner 无法创建 claim；completed/failed claim 返回既有 result、不重放 handler；executing 但无 result时 private state 变 needs_review、public status 仍 waiting并保留 audit/trace。
- [x] 2.6 测试 approved continuation 返回确定性 failed result：handler 恰好一次、run 唯一 failed terminal、public approval 最终 approved、private lease封存、仅一个有效 resolution event/audit，且不进入 needs_review。

## 3. 现有 Eval/Trace seam 扩展

- [x] 3.1 为 `EvalRunner.run_file_dataset` 增加可选 approved case executor protocol，保持无 executor 调用方兼容；测试真实执行、typed expected compare、draft skip 和失败 case。
- [x] 3.2 实现 template `ExampleEvalAdapter`，让 score/trace 继续走 `ScoreSink`、`TelemetryFacade` 和 local-first/provider-degraded 语义，测试 provider 失败不丢本地 refs 与 secret redaction。
- [x] 3.3 为模板 CLI/Makefile 追加四示例 run/eval targets 和 `docs/examples.md` 链接，不重写 `service-app-template-surface` 拥有的基础入口。

## 4. RAG assistant 与 Ticket triage

- [x] 4.1 完成 RAG assistant schema/config/executor，强制经过 `RetrievalProvider -> ContextFragment -> ContextAssembler -> ModelProvider`，测试 BM25 citation、持久化 assembly trace、budget truncation、no-source 和 injection chunk。
- [x] 4.2 完成 RAG assistant approved eval cases，并通过 `EvalRunner` + example adapter 证明正常与降级 case 确定性通过。
- [x] 4.3 完成 ticket triage schema/config/executor、结构化分类和 fake model evidence，测试已知类别、优先级、route、低 confidence `unknown/needs_review`。
- [x] 4.4 完成 ticket triage approved eval cases，并通过 `EvalRunner` + example adapter 证明正常与人工复核 case 确定性通过。

## 5. Repo analyst 与 Dev assistant

- [x] 5.1 完成 repo analyst schema/config/executor 和仅 file tool 的公共执行，测试 workspace 内 read/search、越界拒绝、shell 不可见和长输出 `artifact_ref`。
- [x] 5.2 完成 repo analyst approved eval cases，并通过独立临时 workspace 执行正常、越界和长输出 case。
- [x] 5.3 完成 dev assistant schema/config/executor 和 `ToolRegistry` 编排，测试只读 allow、危险动作 waiting checkpoint +真实 approval record、approved continuation 单次执行、deny/fail 与 audit evidence。
- [x] 5.4 完成 dev assistant approved eval cases，并通过独立临时 workspace执行允许、等待审批、批准后真实执行一次、重复 resolve 不重复和拒绝 case。

## 6. Registry、边界与 Phase 验收

- [x] 6.1 新增 registry/CLI contract test，证明四个 P0 descriptor 可见、config/executor/schema/tool/eval refs 完整，且 local `run` 真实输出不再是固定 `fake-ok`。
- [x] 6.2 新增 vendor SDK、ORM、workspace、tool allowlist、dynamic import 和 secret 静态/行为边界测试，证明四个业务 agent 只依赖 `agent_harness` 公共接口。
- [x] 6.3 更新模板 README 与 `docs/examples.md`，说明四个示例的用途、真实 run/eval 命令、安全降级、fake/local 限制和 Phase 12.5 剩余边界；同步把本 change 触及的 registry、auth/approval、tool execution 主规格及 delta specs 改为稳定 capability/endpoint/行为名称，不把 `Phase N` 临时标签归档进长期规格。
- [x] 6.4 运行四示例 targeted tests/evals、local/service smoke、全量测试、quality、build、license、pre-commit 和 OpenSpec strict validation；静态扫描 `API-Contract.md` 及本 change 触及的 main/delta specs，证明没有 `Phase N` 临时标签，并记录可复现证据。
- [x] 6.5 重新验证 `service-app-template-surface` 的 AC-006、全量 OpenAPI drift、approval-gated 公开 `RUN-005` 返回 409且 handler 为 0、workspace 外 wheel-only copy smoke和 latest migration service smoke，再交给后续 scaffold change做最终三 change组合验收。
