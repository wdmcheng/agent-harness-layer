## Context

当前 RUN-001 只在调用方提供 `X-Trace-Id` 时把 trace 写入 run execution context；CLI、测试和部分内部入口默认传入 null。CanonicalEvent、ApprovalRecord、tool/eval DTO 与多个存储列也允许 null。Product Spec 却要求 approval 必须关联 trace，新的 ModelUsageEvidence 也把 `trace_id` 定义为必填。这个差异横跨 API、runtime、worker、approval、event 和 migration，必须先建立单一 canonical trace 所有权。

## Goals / Non-Goals

**Goals:**

- 每个新 root run 在任何持久化业务事件、queue enqueue、tool/model/provider 副作用前取得唯一 canonical `trace_id`。
- local、service worker、checkpoint/resume、approval/audit 与后续 child run 复用同一 trace，不由各层重新生成。
- 让 ApprovalRecord 和 run-scoped CanonicalEvent 的 trace 关联可由数据库与合同测试验证。
- 对历史 nullable 数据执行确定性、幂等 backfill，并保留可审计迁移证据。

**Non-Goals:**

- 不接入 OTel exporter、SaaS provider、采样、baggage 或 trace 查询 API。
- 不改变 approval 状态机、model usage DTO、delegation 执行或 SSE transport。
- 不要求 request 与 trace 一一对应；同一 run 的恢复、审批和 worker 请求可以有不同 request_id。

## Decisions

1. **runtime composition 是 canonical trace 唯一所有者。** RUN-001、CLI 和内部 run create 都把可选 caller trace 交给同一 normalizer；合法且未冲突的值被保留，缺失时生成全局唯一 ID。任何下游服务只能读取已绑定值，不得自行补一个不同 trace。替代方案是在 EventBus 或 provider adapter 首次需要时生成；拒绝，因为 approval、queue 与首个 lifecycle event 可能先发生。
2. **caller trace 受控且不可重绑定。** 非空 caller value 必须满足稳定长度/字符合同；已绑定到另一 root run 时返回结构化 conflict 且零业务副作用。同一 idempotent run 仅在 caller trace 缺失或与首次 canonical trace 相同时安全重放；后续请求携带不同 trace 时返回 `409 trace.idempotency_conflict`，不改写首次绑定且不产生业务副作用。
   - 稳定格式固定为 1..128 个 ASCII 字符，正则 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`；不 trim、不折叠大小写。缺失时生成 lowercase RFC 4122 UUID 字符串。非法格式返回 `422 validation_error`，已绑定其他 root run 返回 `409 trace.conflict`。
   - CLI 公开 option 固定为 `agent-harness run <agent_id> --trace-id <value>`；它不另建 CLI-only trace 规则。非法格式、全局冲突和 idempotency 冲突分别以 `validation_error`、`trace.conflict`、`trace.idempotency_conflict` 写 stderr 并非零退出，成功路径保留既有 run/status/terminal stdout，失败不回显绑定归属。
   - 独立 `run_trace_bindings(trace_id PK, tenant_id NOT NULL, root_run_id UNIQUE)` 原子声明 root lineage 的全局唯一绑定；`tenant_id` 直接持久化，并由 `(root_run_id, tenant_id)` 到 `agent_runs(id, tenant_id)` 的复合外键或等价数据库约束保证租户一致。`agent_runs` 同时以 `(parent_run_id, tenant_id)` 复合自外键拒绝跨租户父子边，并以可延迟的 `(trace_id, tenant_id)` 复合外键或等价事务安全数据库门禁指向 binding，保证 root/child 投影都不能跨租户复用 trace；所需复合唯一键只服务引用完整性，不改变 `trace_id` 全局唯一和 `agent_runs.trace_id` 非唯一语义。绑定归属读取与错误诊断仍按已认证 tenant 限定；副作用前门禁只允许做不返回 tenant/root 信息的全局存在性判断。runtime 先做无副作用预检以固定候选 trace，再按固定顺序取得幂等键锁与全局 trace 锁，并在锁内复检；该锁覆盖 permission、guardrail/audit 与 root claim，使不同 tenant 或不同 idempotency key 的同 trace 竞争方在任何业务副作用前失败。`agent_runs.trace_id` 是可索引的 lineage 投影，并与 `execution_context_json.trace_id` 逐值一致。child 复用 root binding；只把 trace 放在 JSON 中无法在并发创建时证明 root 间唯一或 child 的租户一致性。
3. **execution context 是跨进程传播源。** canonical trace 与 identity/request 一起写入私有 run execution context；queue message 继续只携带稳定 ref，worker 从持久化上下文恢复。checkpoint/resume、approval、tool/model 和 event service 都从该上下文继承。
4. **公开 DTO 只收紧既有字段。** ApprovalRecord 的既有 `trace_id` 从 nullable 改为 required，不增加第二字段。RunCreateResponse 暂不扩张；调用方通过自己的 header 或 RUN-003 events 获取关联。ModelUsageEvidence 直接消费 canonical trace，不承担生成所有权。
5. **历史关系数据 backfill 先选择 lineage 现值，只有完全缺失时才生成。** Alembic migration 从每个历史 run 沿 `parent_run_id` 找到 root ancestor，并在任何 DDL/UPDATE 前收集该 root lineage 全部 run-scoped canonical trace 候选。每个非空候选都必须匹配新入口的同一 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` 格式；存在非法值立即整批 fail closed，不 trim、不映射、不覆盖。去重后恰有一个合法值时，把它作为该 lineage canonical trace并只填充空值；完全没有候选时才以固定 namespace + root run id 生成 lowercase UUIDv5；存在两个及以上不同合法值时 fail closed。所有被保留或生成的值还必须在不同 root lineage 间全局唯一，碰撞同样在 mutation 前拒绝。root 与所有 child 共用最终值。孤立 parent、lineage 环或跨 lineage trace 复用也整批拒绝。migration 创建带直接 tenant 归属的 binding，并把 canonical 值写入 `agent_runs.trace_id`、私有 execution context、checkpoint state、approval、run-scoped event/envelope、audit payload、tool invocation、eval case/run/score 与 `trace_refs.trace_id`。只有自身 `run_id` 非空的 `eval_runs` 才投影该 AgentRun lineage；聚合多个 case/source run 且自身没有 `run_id` 的 eval run 属于非 run evidence，保持 `trace_id=null`，来源关联继续由各 case/score 的 `run_id`、`trace_id` 和 refs 表达，绝不任选一个 case 或伪造独立 AgentRun binding。provider 的 `trace_refs.external_trace_id` 保持原义且绝不覆盖。重复执行得到同一结果。
   - 新记录必须带显式 `record_scope=run|non_run` 或使用 repository 的等价 typed discriminator；`record_scope=run` 时 `run_id` 非空且必须解析到 AgentRun，`record_scope=non_run` 时不得触发 lineage backfill。一般 legacy record 仍以非空 `run_id`，或 audit payload 明确声明的非空 `run_id` 判为 run-scoped。历史 ordinary TelemetryFacade JSONL 是已知例外：其 envelope `run_id` 可能由 `trace_id` 或字面值 `"telemetry"` 合成，migration MUST 以 `payload.telemetry.context.run_id` 为权威真实归属；该 nested 值非空才是 run-scoped，否则无论合成 envelope 值为何都属于合法 non-run telemetry。`run_id` 为空的人工 eval draft、auth/policy audit、tool/trace/eval evidence 属于合法非 run 记录，保持 nullable 且不参与 lineage；只有显式或按上述 legacy 规则声明了真实 run 归属却无法找到对应 run 的记录才按 orphan fail closed。
   - lineage preflight 还必须逐边验证 child 与 parent 的 `tenant_id` 相同；任一跨租户 parent edge 与孤立 parent、环和 trace 冲突一样，在任何 DDL/UPDATE 前整批 fail closed，不得把 child 归入其他租户的 root binding。
6. **迁移完成后禁止新 null。** 数据库对 approvals 的 trace 列收紧非空；其他可承载非 run 事件的通用表保留 nullable schema，但 repository 对 run-scoped 写入强制非空。这样不把非 run telemetry 伪装为 run trace，又能对 Product P0 路径形成硬门禁。
   - `CanonicalEvent.run_id` 在公开 envelope 中仍是事件 stream 标识；关系库 `canonical_events.run_id` 只保存真实 AgentRun ownership，另以非空 `stream_id` 保存公开 envelope 的 stream 值。`record_scope=run` 要求 DB `run_id` 和 canonical trace 都非空，并以可延迟的 `(run_id, tenant_id, trace_id)` 复合外键指向 `agent_runs(id, tenant_id, trace_id)`，避免直接 SQL 把任意三个各自合法的值拼成错误归属；`record_scope=non_run` 要求 DB `run_id=null`，复合外键按 `MATCH SIMPLE` 允许空 ownership，从而允许字面值 `telemetry` 或 trace-shaped stream 而不伪造 lineage。序列唯一性按 `(tenant_id, stream_id, seq)` 建立；PostgreSQL 对 run stream 锁 AgentRun row，对 non-run stream 按 tenant + stream 取事务级 advisory lock，二者共用 event-id replay、terminal 和 visibility 门禁。`read(run_id=...)` 只查询真实 AgentRun ownership，避免合成 non-run stream 在租户间产生歧义。
   - 相同 `event_id` 只有在稳定事件语义完全一致时才是合法重试。Local/PostgreSQL sink 共用完整 envelope 指纹，只排除 sink 分配的 `seq` 与调用方重建重试时不稳定的 `timestamp`；`event_type`、版本、payload/ref/checksum、identity、parent、request/span/raw ref、scope、terminal、visibility、run/tenant/trace 任一不同都返回脱敏 replay conflict，并在 artifact materialize 与 fan-out 前失败。状态已提交后的 terminal/approval 恢复先读取并验证既有确定性 evidence，只在缺失时补写，不能用新的 request_id 重构同一 event-id。
7. **历史 local JSONL 使用显式 inventory 与可恢复离线迁移。** Alembic 事务只覆盖 SQLite/PostgreSQL rows，不能声称包含 append-only JSONL。新版本的每个 local event/score sink 在首次写入前把 canonical resolved path、kind、format version 和关联 state-dir 注册到持久化 `local-state-manifest.json`，并让新 ordinary telemetry 写入显式 `record_scope`，不再只靠合成 envelope `run_id` 表达归属；旧自定义路径必须由操作者通过单一 `agent-harness migrate-local-state` 离线命令的重复 `--event-path`/`--score-path` 参数显式加入 inventory。命令要求 `--state-dir`，并必须且只能选择 `--profile <name> [--profiles-dir <dir>]` 或 `--file-only`：profile 模式经 typed settings 解析关系库配置并冻结、迁移关系库 + manifest + 显式路径完整 bundle，credential 只能来自环境或受信 `_FILE`，完整 DSN 不得进入 argv、进程列表、shell history、日志或错误；file-only 模式允许显式 non-run records，以及 nested `payload.telemetry.context.run_id` 为空的 legacy ordinary telemetry，即使其 envelope `run_id` 是 trace id 或 `"telemetry"`；任何真实 run-scoped record都 fail closed并要求改用 profile 模式。命令在 state-dir 锁内先完整预检全部 inventory、路径去重、scope 判别与所选模式，再写 migration journal、逐文件备份与同目录临时文件，以确定性 trace 重写 profile 模式中的真实 run-scoped legacy records，并用 fsync + atomic rename 提交。普通 run/eval/API/worker 入口发现旧 schema 或未迁移 inventory 时必须 fail closed，不得自动推进 `0013`。中断或任一步失败时，下一次离线命令必须按 journal 自动恢复到全旧或继续到全新状态，不得把部分重写文件当作完成。已完成 journal 可幂等重放；所有原始备份在明确成功前不得删除。manifest/inventory 之外的任意历史文件不在已证明迁移集合内，命令必须在输出中明确范围，不得声称扫描了任意文件系统位置。

8. **已发布 revision 不改写，shape 漂移只线性前滚。** 早期 service 数据库已 stamp `0013_run_trace_correlation`，但 `canonical_events` 只有 `record_scope`，仍把公开 stream 暂存在非空 `run_id`，且缺少 `stream_id`、tenant/stream 序列唯一键、scope CHECK、三列 run-owner FK、audit scope CHECK 与对应 agent run 引用键。追加 `0013a_run_trace_event_hardening`（`down_revision="0013_run_trace_correlation"`）作为唯一 head。它只接受现场旧 `0013` 完整签名或当前最终 `0013` 完整签名：旧签名先在任何 DDL/DML 前验证 scope、stream、run/tenant/trace ownership、序列唯一性和 audit scope，再确定性复制旧 `run_id` 到 `stream_id`、清空 non-run 数据库 ownership并补齐最终约束；最终签名只验证并 no-op；任何混合、部分或不兼容 shape 零变更拒绝。`0013a -> 0013` downgrade 只回退 revision stamp并保留硬化 schema/evidence，真正的破坏性 `0013 -> 0012a` 仍由 `0013` 的精确 opt-in 与空 evidence 门禁负责。Phase 13.7 的 `0014` 必须直接依赖 `0013a`。

## Affected Surfaces

- runtime run create、execution context、checkpoint/resume 与 queue/worker recovery。
- approval service/repository/API/CLI、CanonicalEvent/EventBus、audit/trace refs。
- RUN-001 可选 `X-Trace-Id`、CLI-RUN-001 可选 `--trace-id` 校验和统一错误映射。
- Alembic migration、SQLite/PostgreSQL repository contracts 和 service smoke。
- 后续 `model-usage-evidence`、`agent-delegation-execution` 与 `sse-event-streaming` 的前置关联合同。

## Testing Seams

- API/CLI/内部入口缺失 trace 时生成、显式合法 trace 时保留、非法或冲突 trace 时零副作用失败；CLI 还逐项断言 `--trace-id`、stderr/exit/stdout 与 side-effect count。
- local 与 service queue/worker 读取同一 trace；restart、checkpoint、approve/deny/resume 后不改变。
- ApprovalRecord、audit 和所有 run-scoped CanonicalEvent 与 persisted run context 逐值一致。
- SQLite/PostgreSQL migration backfill 幂等；已有 trace 不改写；同一 run 的冲突非空 trace 与孤立数据都整批 fail closed。
- model usage 后续 change 只能消费 canonical trace，不能生成第二 trace。

## Risks / Trade-offs

- [Risk] 历史 JSON execution context 与多表 backfill 容易部分成功 → Alembic migration 在单个关系库事务内按 lineage 更新，失败整体回滚，并增加双数据库合同。
- [Risk] 历史 child lineage 存在孤立 parent 或环 → preflight 在任何 DDL/UPDATE 前解析 root ancestor；孤立、环和跨 lineage 冲突全部拒绝，错误只输出脱敏标识。
- [Risk] 无约束的旧 `parent_run_id` 可能跨 tenant → preflight 逐边校验 tenant，迁移后以复合自外键和 trace binding 租户门禁同时阻止 repository 与直接数据库绕过。
- [Risk] provider external trace 与 canonical trace 语义混淆 → `trace_refs` 新增独立 canonical `trace_id` 列，`external_trace_id` 始终保留 provider 原值。
- [Risk] SQLite 与 local JSONL 无跨资源事务，且历史自定义路径不可自动穷举 → sink manifest + 显式 legacy inventory 定义可证明集合；离线 upgrader 使用 state-dir lock、journal、备份和同目录原子替换，不把 Alembic PASS 或未登记文件冒充已迁移。
- [Risk] caller 重用 trace 可能打破全局唯一性 → 建立唯一绑定检查；同一 idempotent run 只在 trace 缺失或相同时复用原绑定，不同 trace 以 `409 trace.idempotency_conflict` 拒绝。
- [Risk] 强制 trace 会暴露此前未覆盖的内部入口 → 所有入口复用同一 normalizer，测试禁止直接构造 nullable ApprovalCreate。
- [Risk] 通用 event 表无法全列 NOT NULL → 只对 run-scoped repository 写入建立硬门禁，非 run telemetry 保留独立语义。
- [Risk] 同一 `0013` revision 在早期 service 与当前 fresh 安装上存在不同物理 shape → 不修改已应用 revision；`0013a` 精确识别两种完整来源并前滚，混合 shape 在 mutation 前拒绝，普通入口按新 head 早期失败。

## Migration Plan

先完成 `embedding-cache-tenant-isolation` 的插入式 revision `0012a_embedding_cache_tenant_scope`，再以 `0013` 增加关系数据 backfill、local JSONL 离线 upgrader 和 repository 非空门禁；随后以 `0013a_run_trace_event_hardening` 统一早期已 stamp `0013` 与 fresh `0013` 的事件 shape，并把它设为 Phase 13.7 `0014` 的直接前置。`0013a -> 0013` 只回退 stamp、不移除硬化 schema；继续执行 `0013 -> 0012a` 时仍要求 evidence 全空和精确 `-x allow_empty_evidence_downgrade=true`，参数缺失、重复、值非法或存在 canonical trace evidence 都必须在 DDL 前拒绝。`0012a` 自身的降级仍由 embedding cache evidence 门禁独立控制。local JSONL 只通过 journal/backup 恢复，不覆盖成功后的新 evidence。完成后停在 `ready-to-archive`，不自动归档。

## Open Questions

无。外部 provider trace 映射与采样策略不属于本 change。
