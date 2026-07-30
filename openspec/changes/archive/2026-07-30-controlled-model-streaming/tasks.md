## 1. 先行 API 契约与供应商 public-seam 红测

- [x] 1.1 在实现前更新 `API-Contract.md` 5.9.1 MOD-004，冻结 provider seam、64/65 容量、stream/usage 的 128 字符内稳定 identity、payload、可见性、跨块安全、背压、独立时延证据、顺序、unknown、恢复与 transport 规则。
- [x] 1.2 为 `ModelStreamingProvider`、`PreparedModelStreamCall`、`ModelStreamDelta`、`ModelStreamUsage` 与 `ModelStreamCloseResult` 编写导入级和行为级红测，逐值覆盖 not_started/stopped/unknown 与 null/partial/complete usage 组合、SDK usage 隔离、单个任意大 fragment 在 collector 前拒绝，并证明既有 `complete` seam 不变。
- [x] 1.3 在 `tests/contracts/test_controlled_model_streaming_routing_contracts.py` 为 `_router_current.py` 当前配置规划、`_router_snapshot.py` 冻结快照恢复、router `text_stream` capability、无副作用 `prepare_stream`、不支持 provider 的副作用前拒绝与禁止 complete fallback 编写红测；逐值证明 `text_stream` 往返、`text_completion` 回归不变、未知 capability 关闭失败，并断言 provider call count。
- [x] 1.4 为目标分片 1～4096、敏感候选 128～4096、固定 64/65 硬合同和非法环境配置关闭失败编写 typed-config 红测。
- [x] 1.5 为 deterministic fake stream 编写成功、已证明取消、unknown、deadline 与慢消费脚本化红测，固定 fragment、pull-started、pause gate、指定 ordinal 失败与 close result；保证不依赖 sleep/网络且默认不读取凭证。
- [x] 1.6 通过 `build_execution_context()` 取得 `BoundModelInvocationService` 编写生产入口红测，覆盖普通 `stream`、审批 `stream_approved`、可信 `operation_key`/`approved:{approval_id}` identity、未批准/过期/重放和 approval/lease/tenant/identity/agent/run/action/resource/arguments hash 九字段不匹配的零容量与零 provider 副作用，并证明业务 executor 不能传入 `usage_call_id` 或取得未绑定 service。

## 2. 容量、outbox 与终态围栏的 public-seam 红测

- [x] 2.1 在 local seam 编写同事务双预留、65 个稳定占位、真实事件消费、未用占位取消/精确释放与 high-water/outstanding 红测。
- [x] 2.2 在 SQLite repository seam 编写最大长度 tenant、stream group/delta/completed 与 `stream-usage-v1` started/final 五种定长 identity、legacy usage recovery、顺序、`started → result_persisted → published`、非法事件拒绝、幂等重放冲突，以及前驱缺失/重复/非连续关闭失败红测。
- [x] 2.3 在真实 PostgreSQL migration head 上编写并运行 stream 合同，覆盖最大长度 tenant/ordinal 64 的五种 stream/usage identity、legacy recovery、并发 claim、行锁、原子回滚、占位取消/释放、pending 查询、terminal guard 与前驱缺失关闭失败；重复 sequence 由数据库唯一约束拒绝，非连续前缀由共享 validator 拒绝。证据必须显示使用真实 PostgreSQL 而非 SQLite 替代。
- [x] 2.4 编写 stream/usage 任一 pending、unknown、completed 发布失败或 usage 未发布时拒绝所有 run terminal 的红测，以及全部安全结算后放行的对照用例。

## 3. 分片、安全和事件顺序的 public-seam 红测

- [x] 3.1 为任意供应商边界到 1～4096 UTF-8 bytes 公共分片、最多 64 条、非空 delta、连续 ordinal、稳定 event id、最终字节数/SHA-256 一致编写 AC-085 红测；provider-neutral 最终文本与已观察 delta 不一致时，即使关闭 seam 回报 stopped+complete 计量，也必须强制 unknown、保留未决容量并禁止 completed/final usage/terminal。
- [x] 3.2 为 `sk-`、authorization、cookie、api_key、password、secret、token 在每个字符边界跨块，以及长敏感候选溢出不泄漏且 retained bytes 不超过配置硬上限编写 AC-088 红测；另逐值覆盖 `OPENAI_API_KEY`、`db_password`、`client_secret`、`access_token`、authorization 值以既有正则允许字符起始的边界、cookie/set-cookie 空值与分号起始值、scheme-only `authorization: Bearer|Basic `，以及 ASCII/Unicode 词字符、标点和空白位于 authorization/cookie 左侧的 `\b` 语义，断言 guard 与既有 `redact_secrets()` 同义且 durable outbox/公共 delta 均无原值。第二十九名 Reviewer 1 的 cookie/hard-bound 红灯与第三十名 Reviewer 1 的 fragment 左边界红灯均已闭合；最新安全文件 39/39、1,409 条文本/31,716 次全 split 与逐字符差分 0 mismatch。职责迁移后 streaming 聚焦 glob 为 174 collected / 168 passed / 6 skipped，独立 pipeline 合同文件 7/7 PASS。
- [x] 3.3 为完整结果 guardrail 禁止 speculative delta、成功后再分片和拒绝时零公开输出编写红测。
- [x] 3.4 为 started → delta* → completed → usage → terminal、完整唯一 `1..n-1` 前驱 outbox 围栏，以及 stopped+complete usage、stopped+partial、unknown+partial、取消/deadline 不伪造完成/零用量/终态编写 AC-086 红测；prepare 消耗大部分预算后，尾部/full-result delta 发布必须仍受同一绝对 route deadline 约束。不可信计量必须覆盖 partial 的单边/双边 token、`finality=complete` 但缺 input/output，以及启用成本路由缺可信 cost，逐值断言 usage、共享预算 operation/owner 同事务 needs-review、unknown charge、66 个剩余容量槽保留与 exact replay 零二次 provider 调用。
- [x] 3.5 为 delta `result_persisted` 后 persist commit-ack 丢失、公开失败/崩溃、completed 首次公开失败、稳定 envelope 补投、禁止 provider 重启与禁止新 ordinal 编写恢复红测；即使 intent 已提交而本地 chunk 计数仍为 0，也必须扫描完整 group、按 durable chunk 进入 needs-review，不能按偏小本地/公开计数取消槽位。成功最终值必须先把 completed intent、usage result、shared-budget settlement 和尾部释放放进同一 UoW，恢复再按 completed → usage 补投。
- [x] 3.6 以 fake clock 为 `model-stream-live-smoke/v1` 编写红测，逐字段验证真实 RUN-006 ASGI SSE request 的已有事件首 frame、provider 首 delta、首个 committed delta、客户端收到 delta 的非负毫秒值、成功顺序、脱敏与 hosted-unverified/failed/external-blocked 状态映射；本地 contract failure 必须通过 executor 实际捕获的封闭 failure domain 保留已观察 provider response/delta 的调用事实，不得伪造 response 前提或以 sink 直读冒充 SSE 指标。
- [x] 3.7 为双预留和 started 已提交、首次 SDK context/provider 迭代前取消或 deadline 耗尽的完整窗口编写红测，包括 durable started 后 telemetry fan-out 等待点，以及 telemetry 完成后由 runtime `asyncio.timeout_at` 在阻塞 prepare 中自然到期、不得外部 `task.cancel()` 或篡改 adapter deadline 的节点；fake 与真实 `PydanticAIModelProvider` 均断言 started/high-water 保留、零 SDK context/provider 调用、65 个占位取消且只释放 outstanding、not-started cancelled usage final 消费第二槽。真实 adapter 节点必须在 client acquisition/permit seam 自然超时，并以同一 provider 再次成功取得 prepared stream 证明 permit 已释放；预算/lease 释放后才允许 run cancelled。
- [x] 3.8 为 `models/_settlement_evidence_validation.py` 的结算、重放与恢复 route evidence 编写红测：合法 `text_stream` 与 `text_completion` 逐值通过，未知 capability、快照/当前 route 不一致及篡改字段关闭失败，且不重新调用 provider。

## 4. 供应商协议、配置与 fake 实现

- [x] 4.1 实现供应商中立 stream/close/partial-usage DTO/Protocol、router capability 与 `prepare_stream`；同步更新 `_router_current.py` 和 `_router_snapshot.py` 的精确 capability 校验，通过 1.2～1.3 并保持现有 provider/router 测试通过。Pydantic stream context 返回值精确绑定并导出 `StreamEventContext`；Pyright 静态夹具同时证明锁定 SDK、本地正确 double 兼容，错误 shape 不兼容，不能用只排除 `Any` 的结构断言冒充窄类型门禁。
- [x] 4.2 实现受约束 typed config 和 runtime composition 注入，固定版本化 64/65 合同，通过 1.4 且不改变默认 fake provider。
- [x] 4.3 实现 deterministic fake stream 与关闭状态控制，通过 1.5，并提供不依赖 sleep/网络的可控背压和崩溃 seam。

## 5. 流事件容量与持久化实现

- [x] 5.1 新增 `MODEL_STREAM` 注册容量 65、local/SQL 双 claim 与未消费容量释放协议，通过 2.1 和容量注册不变量测试。
- [x] 5.2 以 `stream_evidence_repositories.py` mixin 扩展既有 `EvidenceOutboxRepository`，复用同一 UoW `evidence_outbox` / `AsyncSession` 实现 64 delta + completed 的批量占位、payload 固化、顺序发布、批量取消和稳定重放，通过 2.2；不得另造事务或隐式增加 UoW repository 属性。
- [x] 5.3 将 stream pending、unknown、outbox 与容量状态接入 `events/local_capacity.py`、`events/sinks/postgresql.py` 和 run terminal guard；两个 sink 均须在写事件与递减 outstanding 前原子核对 stream event id、group、sequence、event type 与 payload identity，通过 2.3～2.4。若现有 schema 足够，提交无迁移的验证证据。

## 6. 安全分片与 invocation 编排

- [x] 6.1 实现跨块 incremental text guard，复用现有敏感模式语义并对候选溢出 fail closed，通过 3.2～3.3 和现有 redaction 回归测试；authorization 独立终止集合兼容 scheme 分隔符回退，cookie 按既有正则区分空值、前导 whitespace、分号与值后换行，feed 在追加下一字符前守住 retained UTF-8 bytes 硬上限；已发布安全前缀的 Unicode 词字符状态继续参与 authorization/cookie 左侧 `\b` 判定，不能在 fragment 切点伪造新边界，也不能给无左边界的通用 key 规则加限制。
- [x] 6.2 实现 Unicode 安全的有界分片、无整块 bytes 复制的 provider delta/collector 上限、缓存 byte count、增量 SHA-256、稳定 delta/completed payload 与最终文本一致性检查，通过 3.1。
- [x] 6.3 在独立 streaming mixin 中实现 `ModelInvocationService.stream` 的双预留、started、provider 迭代、outbox、completed、usage 与 budget/lease 结算；进入 prepare 前建立唯一绝对 route deadline，覆盖 prepare、消费、完整结果 guardrail、尾部分片与 delta 持久化/发布，不在 prepare 后重启 timeout。复用 `_settlement_publication.py` 的私有 UoW seam 原子持久化 completed intent、usage result、预算和尾部释放；在 `BoundModelInvocationService` 暴露可信 `stream` / `stream_approved`。审批入口复用既有 durable grant 全绑定、单次 lease 与 current hard-gate 重检，通过 1.6、3.4 和 3.5，且不引入 retry/fallback。
- [x] 6.4 实现已证明停止与 unknown 的取消/deadline/存储失败分类；仅 stopped+complete、input/output token 与当前路由启用的 cost 维度全部可信，且不存在已耐久未公开 delta 时可结算。delta publish 未闭合、null/partial 或虽标记 complete 但维度不完整的 usage 均以封闭 attempt review 在同一 UoW 将 usage outbox、共享预算 claim/allocation 与 owner ledger 提升为 needs-review，保留全部未决容量并围栏 exact replay；同步更新 `_settlement_evidence_validation.py`、`usage_evidence_repositories.py`、同 `_session` 的 `usage_attempt_review_repository.py` 与 `_shared_budget_replay_repository.py`，使结算/重放/恢复只接受精确 `text_completion` / `text_stream` route 和匹配 review evidence，通过 3.8 与 AC-086 全部负路径。

## 7. Pydantic AI 适配与资源生命周期

- [x] 7.1 为锁定 Pydantic AI 2.5.0 的 `run_stream_events` 编写 adapter contract tests，覆盖 zero-buffer backpressure、文本 start/delta、非文本隔离、唯一 final、缺失/重复 final、context 清理、高碎片线性累积不变量、deadline 在 context 创建前耗尽的 durable not-started 收口，以及 SDK usage accessor 抛异常和 bool/负数/非整数的公开 invocation needs-review 负路径。
- [x] 7.2 实现 Pydantic AI stream prepare/adapter，只输出文本增量和最终 `ModelResponse`，SDK usage 只读一次并缓存 provider-neutral 转换；读取/校验失败时 result 稳定 unknown、close 安全且不二次读取。禁止 `stream_text(delta=True)`、SDK 类型外泄和 provider cursor，通过 7.1 与现有一次性 adapter 测试。
- [x] 7.3 将 stream context、后台 task、client/permit lease 接入 runtime close 顺序，区分“已请求迭代”与“SDK context 已创建”，验证正常完成、context 创建前 deadline、提前取消、context 创建后 deadline 和 composition 关闭均无资源泄漏；context 创建前保持 not-started，未知远端不被误判为 stopped。活动 context 与 prepare-close 盲区已红→绿；第二十七名 Reviewer 1 证明并发 `ModelRouter.aclose()` 会提前返回后，public service 并发 close 节点先稳定 RED。router 现让所有调用等待同一 provider 完成事件，并缓存首次失败事实；并发成功、provider close 失败与首个关闭者取消三个节点均转绿，后续 close 不会伪装成功。composition 文件 6/6、受影响 12 文件与 quality 719 files 全绿。
- [x] 7.4 实现独立 live stream smoke artifact、真实 service-app ASGI SSE 首 frame 探针与四前置状态机，通过 3.6；本地 setup/start-run/terminal/capacity/shared-budget/publication/policy/guardrail/probe/cleanup 失败均由完整受控执行边界输出 `failed/contract_failure` 与退出 1。`RunOrchestrator.start_run()` 异常是独立本地事实，即使业务 executor 返回 provider-domain 错误且 probe/cleanup 成功也由本地合同失败最终归因并保留调用事实；provider/committed/client 时钟建立确定 happens-before。只有不存在任一本地失败且 `failure_domain=provider` 的稳定 provider/network 故障输出 external-blocked。结果契约、时延探针、composition 与 CLI 分责，默认路径只跑 fake clock contract，真实 endpoint 仍需本会话授权且不得输出 prompt、文本、secret、failure domain 或 raw provider error。

## 8. 恢复、SSE/CLI 与完整验证

- [x] 8.1 实现 stream outbox recovery 与 needs-review 分类，只补投 durable `result_persisted` 事件并在安全条件满足后恢复 usage/terminal，通过 3.5。
- [x] 8.2 为 SSE `Last-Event-ID`、CLI `--after-seq`、public/internal filter 与 reader disconnect 编写并通过 AC-087 测试，断言重连和断开均不改变 provider call count。
- [x] 8.3 运行 streaming 定向测试、全量 pytest、mypy、ruff、核心 smoke、service smoke、`openspec validate --all --strict`、local/SQLite/真实 PostgreSQL 矩阵；实现候选 `361678bf…` 静态 `1+2` 后只运行一次最终重型门禁：真实 PostgreSQL 5/5、全量 `1712 passed / 230 skipped in 899.54s`、eval 11/11、local fake 1.889 秒、service smoke、build 与 license PASS。AC-065 同一全量运行通过且未改阈值。已授权 live stream/completion 分别以 `credential_missing` / `typed_preflight_missing`、`provider_called=false` 诚实记录 hosted-unverified，无网络与 token 消耗。
- [x] 8.4 核对实现与先行 `API-Contract.md` 5.9.1 无漂移，并更新 living plan 与 change matrix 的 Progress、Discoveries、Decision Log、唯一下一动作和 Handoff Snapshot，保留可复核命令、结果与冻结 diff 身份。实现候选 `361678bf…` 已由 Reviewer 1/2/3 同身份 Stage 1/2 PASS、0 findings并通过最终重型门禁；当前只冻结最终证据文档身份并做 final evidence `1+2`，通过后写 `clean`，停在 ready-to-archive。
- [x] 8.5 按 design 的精确 owner/producer 表逐路径核对生产、测试、`models/_router_current.py`、`models/_router_snapshot.py`、`models/_invocation_streaming.py`、`models/_streaming_contracts.py`、`models/_streaming_consumption.py`、`models/_streaming_events.py`、`models/_streaming_settlement.py`、`models/_settlement_contracts.py`、`models/streaming.py`、`models/_settlement_publication.py`、`models/_settlement_validation.py`、`models/_settlement_evidence_validation.py`、`adapters/models/_pydantic_ai_streaming.py`、`events/sinks/_postgresql_streaming.py`、`storage/stream_evidence_repositories.py`、`storage/usage_evidence_repositories.py`、`storage/usage_attempt_review_repository.py`、`storage/_shared_budget_replay_repository.py`、SDK event test helper、静态类型夹具 `tests/contracts/controlled_model_streaming_context_typecheck.py`、拆分后的 runtime/capacity contract 文件、`tests/contracts/test_controlled_model_streaming_runtime_composition_close_contracts.py`、三个既有 recovery fixture、审批 composition 支持/合同文件、`tests/contracts/test_controlled_model_streaming_routing_contracts.py`、`scripts/live_model_stream_contract.py`、`scripts/live_model_stream_probe.py`、`scripts/live_model_stream_execution.py`、`scripts/smoke_live_model_stream.py`、`Makefile`、`.github/workflows/ci.yml`、`.gitlab-ci.yml`、`compliance/ci-jobs.toml`、`scripts/ci_evidence.py`、`tests/contracts/test_ci_pipeline_contracts.py`、`docs/acceptance-matrix.md`、`Product-Spec.md` 与 `Product-Spec-CHANGELOG.md`；双 CI `acceptance-validate` 必须把 stream smoke job 加入 `needs` 并下载其安全 artifact。实现期与 Reviewer 1/2/3 修复期发现的清单外 owner 已补回 design/change matrix/DEV；最终 Reviewer 1 必须对修复后的完整内容重新审查。
