## Source Links

- Product-Spec.md：Phase 18「受控真实模型调用」与 REQ-026 / AC-085～AC-088。
- Product-Spec-CHANGELOG.md：Phase 18.1 增量文本流式输出的产品契约记录。
- DEV-PLAN.md：Phase 18.1「受控真实模型增量文本流式输出」。
- API-Contract.md：5.9.1 MOD-004 先行 producer 契约、事件容量不变量、RUN-006 与 CLI-EVT-001。
- docs/plans/architecture-evolution-plan.md：7.3「Phase 18.1 受控增量文本流式输出」。
- docs/plans/architecture-evolution-change-matrix.md：Phase 18.1 单 change、单 owner 的变更边界。
- docs/adr/0001-p0-service-boundaries.zh-CN.md：领域层、应用层、基础设施层与组合根边界。
- docs/adr/0002-vendor-adapter-isolation.zh-CN.md：供应商 SDK 仅存在于适配器层。
- Design-Brief.md 或设计稿：不适用；本变更不新增 UI。
- CONTEXT.md：不存在，沿用现有产品与 API 术语。

## Why

Phase 18 已具备受控的一次性真实模型调用，但调用方仍只能在完整响应落定后获得文本。Phase 18.1 需要在不泄露供应商事件、不绕过事件容量、持久化、用量结算与终态门禁的前提下，交付可恢复、可审计的增量文本输出。

## What Changes

- 新增供应商中立的增量文本协议，只公开有序文本片段和最终结果；Pydantic AI 的事件类型、游标与重试细节不越过适配器边界。
- 在 `build_execution_context()` 已有可信 run 绑定上，为 `BoundModelInvocationService` 增加异步 `stream` / `stream_approved`；普通与审批调用都返回最终 `ModelResponse`，增量只进入 durable CanonicalEvent，业务 executor 不能提供 `usage_call_id` 或绕过 grant/lease。
- 为每次流式调用在供应商副作用前同时预留既有模型用量证据容量和固定上限的流事件容量，并为所有可能的文本片段与完成事件预建稳定身份的 outbox 槽位。
- 新增有界分片、合并、跨供应商分块的敏感内容防护与最终文本一致性校验；无法安全继续时关闭增量路径，不伪造完成、用量或运行终态。
- 固化 `model.output.delta`、`model.output.completed`、`model.usage.updated` 与运行终态的可见性、稳定标识、关联字段和顺序。
- 将显式取消、deadline、客户端慢消费、容量不足、存储故障、进程崩溃与未知供应商结果纳入同一恢复协议；恢复只重放已持久化事件，不重启供应商流。
- 复用既有 SSE `Last-Event-ID` 和 CLI `--after-seq` 已提交事件读取器，使新事件可断点续读；传输断开只停止读取端。
- 默认测试继续使用 fake provider；真实模型流式延迟验证保持显式 opt-in，且不进入默认 CI。

## Non-Goals

- 不新增聊天、WebSocket、浏览器 UI、供应商原生事件透传或供应商游标协议。
- 不改变 Phase 18 已冻结的一次性 `complete` 调用语义，也不为不支持流式的 provider 伪造分块。
- 不自动重试、切换 provider、重新开始供应商流，亦不把未知结果降级为普通失败。
- 不新增动态或无限事件容量，不允许按实际输出在副作用后补预留。
- 不执行真实 provider 请求、部署、发布、OpenSpec sync 或 archive。

## Capabilities

### New Capabilities

- `controlled-model-streaming`：定义供应商中立增量文本调用、固定容量、稳定身份、跨分块安全、持久化顺序、中断与恢复的完整行为契约。

### Modified Capabilities

- `canonical-events-artifacts`：增加流式输出事件的预留、稳定身份、outbox 槽位、可见性与终态围栏要求。
- `model-usage-evidence`：规定流式调用复用同一用量生命周期，并在完成或已证明中断前后保持正确的用量证据顺序。
- `agent-registry-model-context`：增加 provider/router/invocation 的供应商中立流式能力协商与不支持路径。
- `sse-event-streaming`：明确新流式输出事件仅通过已提交事件日志续读，读取端断开不得控制供应商执行。
- `typed-config`：增加受约束的目标分片和安全窗口配置，同时把总增量事件上限固定在版本化容量合同中。

## Impact

- 代码/测试/CI：精确 owner 与 AC-085～AC-088 producer 冻结在 `design.md` 的 Affected Surfaces / producer 表和配套 change matrix 18.1 行，包含可信 bound façade、当前路由与快照恢复、结算证据校验、provider/adapter、capacity/outbox/recovery、SQLite/真实 PostgreSQL、SSE/CLI、独立 live script、Make/双 CI、`compliance/ci-jobs.toml`、CI pipeline contract 与验收矩阵；其中 `models/_router_current.py`、`models/_router_snapshot.py`、`models/_settlement_publication.py`、`models/_settlement_validation.py`、`models/_settlement_evidence_validation.py`、`storage/usage_evidence_repositories.py` 与 `storage/_shared_budget_replay_repository.py` 必须逐值接受受信 `text_stream` route/cancel/needs-review evidence，同时保持非流式行为不变。实现期与 Reviewer 1 修复期发现的 SDK event test helper、三个 completed-recovery fixture、审批 composition 支持/合同文件与 `Product-Spec.md` 状态同步均补入 owner；最终实现审查必须读取修订后的完整内容。
- API/事件：新增并冻结 `model.output.delta`、`model.output.completed` 的 payload、可见性和稳定事件标识；不新增 HTTP 路由。
- 数据：优先复用现有 `event_capacity_reservations` 与 `run_evidence_outbox` 结构；若设计或红测证明现有约束不足，才增加迁移，不能凭猜测改 schema。
- 依赖：继续锁定项目当前 Pydantic AI 版本；SDK 仅在 vendor adapter 内使用。
- 验证：新增 AC-085～AC-088 的 public-seam 红绿测试，覆盖 local、SQLite 与真实 PostgreSQL；默认离线，真实 provider 仅 opt-in。
