## ADDED Requirements

### Requirement: 每个新 root run 在副作用前绑定 canonical trace
系统 SHALL 为每个新 root run 绑定一个非空、全局唯一的 canonical `trace_id`。调用方提供合法且未冲突的 trace 时 MUST 保留；缺失时 MUST 由受控 runtime composition 在创建 run、发布 lifecycle event、enqueue 或调用 tool/model/provider 前生成。任何下游组件 MUST NOT 为同一 run 生成第二个 trace。

Caller trace MUST 匹配 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`，不做 trim 或大小写折叠；缺失时生成 lowercase RFC 4122 UUID 字符串。系统 MUST 通过 `run_trace_bindings(trace_id PK, tenant_id NOT NULL, root_run_id UNIQUE)` 原子声明 root lineage 全局唯一性，直接持久化 tenant，并以复合外键或等价数据库约束保证 `(root_run_id, tenant_id)` 与 `agent_runs(id, tenant_id)` 一致。绑定归属读取与诊断 MUST 使用已认证 tenant 限定；副作用前的全局碰撞门禁只能返回存在/不存在，不得返回其他 tenant/root 信息。系统 MUST 用全局 trace 锁覆盖锁内复检、permission、guardrail/audit 与 root claim，使不同 tenant 或不同 idempotency key 的同 trace 竞争在业务副作用前串行收敛。`agent_runs.trace_id` 是允许 root/child 重复的非唯一投影，并与 `execution_context.trace_id` 双写。非法格式返回 `422 validation_error`；已绑定其他 root run 返回 `409 trace.conflict`。

`agent_runs` MUST 以 `(parent_run_id, tenant_id)` 复合自外键拒绝跨租户父子边，并以可延迟的 `(trace_id, tenant_id)` 复合外键或等价事务安全数据库门禁保证每个 root/child 投影属于同租户 binding。为引用完整性增加的复合唯一键 MUST NOT 改变 `trace_id` 全局唯一和 `agent_runs.trace_id` 非唯一语义。

#### Scenario: 缺失 caller trace 时生成
- **WHEN** 调用方从 API、CLI 或内部受控入口创建 run 且未提供 trace
- **THEN** 系统在首个持久化业务副作用前生成 canonical trace，并把后续 run context、event 与 approval 关联到该值

#### Scenario: 合法 caller trace 被保留
- **WHEN** 调用方提供合法且尚未绑定其他 root run 的 trace
- **THEN** 系统把该值绑定为 canonical trace，后续恢复、worker 和 evidence 不改写它

#### Scenario: 冲突 trace 零副作用失败
- **WHEN** caller trace 已绑定到另一个 root run 或不满足稳定格式
- **THEN** 系统返回结构化 validation/conflict 错误，且不创建 run、event、queue message、approval、guardrail audit 或 provider side effect

#### Scenario: 不同幂等键并发竞争同一 trace
- **WHEN** 两个 tenant 或同一 tenant 的两个不同 idempotency key 并发提交同一合法 caller trace
- **THEN** 全局 trace 锁只允许一个请求进入 permission/guardrail 与 root claim，另一请求在锁内复检时返回 `trace.conflict`，且其 audit/event/queue/provider 副作用均为零

### Requirement: CLI run 暴露同一 caller trace normalizer
系统 SHALL 为 `agent-harness run <agent_id>` 提供可选 `--trace-id <value>`。该 option MUST 原样进入 RUN-001/内部 run create 共用的 normalizer；缺失时生成 canonical trace，提供时使用相同正则、全局 binding 和 idempotency 规则。非法格式 MUST 只向 stderr 写稳定 `validation_error` 并非零退出；已绑定其他 root run MUST 使用 `trace.conflict`，同一 idempotency key 后续提供不同 trace MUST 使用 `trace.idempotency_conflict`。以上失败 MUST 在 run/event/queue/approval/tool/model/provider 副作用前发生，stdout 不得回显其他 root/tenant 的绑定信息。

#### Scenario: CLI 缺失或提供合法 trace
- **WHEN** 调用方执行 CLI run 且省略 `--trace-id`，或提供尚未冲突的合法值
- **THEN** 系统分别生成或保留 canonical trace，并让 run execution context 与后续 evidence 使用该同一值

#### Scenario: CLI 非法或冲突 trace 安全失败
- **WHEN** `--trace-id` 为空白、超长、含非法字符、已绑定其他 root，或与相同 idempotency key 的首次 canonical trace 不同
- **THEN** CLI 以对应稳定 code 写 stderr 并非零退出，stdout 不泄露绑定状态，所有业务副作用计数为零

### Requirement: canonical trace 跨恢复和进程边界保持不变
系统 SHALL 把 canonical trace 持久化在 run execution context，并让 local execution、service queue/worker、checkpoint/resume、approval/audit、tool/model evidence 和 terminal event 只从该上下文继承。不同入口请求可以使用不同 `request_id`，但同一 run 的 canonical `trace_id` MUST 保持不变。

#### Scenario: Worker 重建后恢复同一 trace
- **WHEN** API 提交 run 后由另一个 worker 进程读取持久化 execution context 并执行
- **THEN** queued、started、tool/model 与 terminal evidence 的 trace 与 run 创建时逐值一致

#### Scenario: Approval resume 不重新生成 trace
- **WHEN** waiting run 跨进程重启后被 approve 或 deny 并恢复 continuation
- **THEN** approval、audit、resume 和 terminal evidence 保留原 canonical trace，即使这些操作使用新的 request_id

### Requirement: 历史 nullable trace 数据确定性迁移
系统 SHALL 按 root run lineage 为历史 nullable trace 数据执行确定性、幂等 backfill。Migration MUST 在任何 DDL/UPDATE 前收集 lineage 中所有 run-scoped canonical trace 候选：任一非空值不匹配 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` 时整批 fail closed；去重后恰有一个合法非空值时 MUST 保留该值并只填空项；没有非空值时 MUST 以固定 namespace 与 root run id 生成 lowercase UUIDv5；存在多个不同合法值时 MUST fail closed。最终值还 MUST 在 root lineage 间全局唯一，保留值或生成值与其他 lineage 碰撞时不得提交。root 与所有 child run 的 binding、`agent_runs.trace_id`、execution context、checkpoint state、approval、run-scoped event/envelope、audit、tool invocation、eval case/run/score 和 `trace_refs.trace_id` MUST 获得该同一最终值。只有 `eval_runs.run_id` 非空时，`eval_runs.trace_id` 才 MUST 直接投影该 AgentRun canonical trace；自身无 `run_id` 且聚合多个 case/source run 的 eval run MUST 保持 `trace_id=null`，来源关联由 case/score refs 表达，migration MUST NOT 任选一个 case 或生成不属于 AgentRun lineage 的 binding。`trace_refs.external_trace_id` 是 provider 外部标识，MUST NOT 被 canonical trace 覆盖。新 record MUST 使用显式 `record_scope=run|non_run` 或等价 typed discriminator；run-scoped record必须有能解析到 AgentRun 的真实 `run_id`。一般 legacy record 以非空 `run_id`，或 audit payload 明确声明的非空 `run_id` 判为 run-scoped；历史 ordinary TelemetryFacade wrapper MUST 改用 `payload.telemetry.context.run_id` 判定，nested 值为空时即使 envelope `run_id` 是 trace id 或 `"telemetry"` 也属于 non-run evidence。没有真实 run 归属的人工 eval draft、聚合 eval run、auth/policy audit及其他 non-run evidence保持 nullable且不参与 lineage。trace 被不同 root lineage 复用、parent 孤立、lineage 成环或已声明真实 run 归属的记录无法找到对应 run 时，关系数据 migration MUST 在单事务内 fail closed，不得选择任一值、覆盖、部分提交或删除 evidence。

关系 migration 还 MUST 在任何 DDL/UPDATE 前逐边验证 parent/child `tenant_id` 一致；任一跨租户 parent edge 必须与孤立 parent、lineage 环和 trace 冲突一样整批 fail closed，不得生成 binding、回填 trace 或提交部分修改。

#### Scenario: 重复 backfill 结果一致
- **WHEN** migration 在相同历史数据上被验证或重试
- **THEN** 每个 run 得到相同 trace，已有 trace 不被覆盖，所有关联记录逐值一致

#### Scenario: 单一合法历史 trace 优先于生成值
- **WHEN** 同一 root lineage 的部分 run-scoped records 为空且其余 records 只包含一个满足新格式的非空 trace
- **THEN** migration 保留该非空值并只填充空项，不另行生成或覆盖 canonical trace

#### Scenario: 全空 lineage 确定生成 trace
- **WHEN** 同一 root lineage 的全部 run-scoped canonical trace 候选都为空
- **THEN** migration 以固定 namespace 和 root run id 生成 lowercase UUIDv5，重复执行得到同一值

#### Scenario: 非法单一历史 trace 阻止迁移
- **WHEN** lineage 只有一个不同非空 trace，但该值不满足新入口格式
- **THEN** migration 在任何 DDL/UPDATE 前整批失败，不 trim、映射、覆盖或生成替代值

#### Scenario: 孤立记录阻止迁移
- **WHEN** approval/event/checkpoint 或其他已声明非空 `run_id` 的 record 无法关联到现有 run
- **THEN** migration 整体失败并报告脱敏记录标识，不提交部分 backfill 或删除历史数据

#### Scenario: 合法非 run evidence 保持独立
- **WHEN** 人工 eval draft、auth/policy audit 或其他 evidence 没有声明 `run_id`
- **THEN** migration 保留其 nullable trace 和独立语义，不把它当 orphan，也不为其伪造 run lineage

#### Scenario: 冲突非空 trace 阻止迁移
- **WHEN** 同一历史 run 的 execution context、approval、event、audit 或 trace record 中存在两个及以上不同非空 trace
- **THEN** migration 整体失败并报告脱敏 run/record 标识，不选择 canonical 值、不覆盖已有值且不提交任何 backfill

#### Scenario: Child run 继承 root trace
- **WHEN** 历史 run 通过无环、完整且每条 parent/child 边 tenant 一致的 `parent_run_id` lineage 归属同一 root run
- **THEN** root 与所有 child 使用基于 root run id 的同一确定性 canonical trace，重复 backfill 结果不变

#### Scenario: 跨租户父子边阻止迁移和新写入
- **WHEN** 历史或新建 child 的 `tenant_id` 与直接 parent、root binding 或 canonical trace binding 的 tenant 不一致
- **THEN** migration 在任何 DDL/UPDATE 前整批失败，repository/database 写入门禁也拒绝该 child，且不创建 binding、run、event 或 queue/provider 副作用

#### Scenario: Provider trace 标识保持独立
- **WHEN** run-scoped `trace_refs` 同时记录 provider `external_trace_id` 和 canonical trace
- **THEN** migration 只填充独立 `trace_id`，不改写 provider `external_trace_id`

### Requirement: 历史 local JSONL 可恢复迁移
系统 SHALL 把 Alembic 事务边界限定为 SQLite/PostgreSQL rows，并通过单一 `agent-harness migrate-local-state` 离线命令迁移 append-only event JSONL 与 eval `scores.jsonl`。每个新 local sink MUST 在首次写入前把 canonical path、kind、format version 与 state-dir 注册进持久化 manifest；新 ordinary telemetry MUST 写显式 `record_scope`，legacy 自定义路径 MUST 通过重复 `--event-path`/`--score-path` 参数显式加入 inventory。命令 MUST 接收 `--state-dir`，并 MUST 且只能选择 `--profile <name> [--profiles-dir <dir>]` 或 `--file-only`：profile 模式通过 typed settings 解析关系库配置，credential 只能来自环境或受信 `_FILE`，完整 DSN MUST NOT 进入 argv、进程列表、shell history、日志或错误；该模式冻结并迁移关系库 + manifest + 显式路径 bundle，完整预检数据库和全部去重文件；file-only 模式允许显式 non-run records，以及 `payload.telemetry.context.run_id` 为空的 legacy ordinary telemetry，即使其 envelope `run_id` 是 trace id 或 `"telemetry"`，发现任一真实 run-scoped record MUST fail closed并要求改用 profile 模式。随后使用 journal、逐文件原始备份、同目录临时文件、fsync 和 atomic rename；profile 模式只重写真正归属 AgentRun 的 records，non-run records 保持原样。普通 run/eval/API/worker 入口发现旧 schema 或未迁移 inventory MUST fail closed，不得自动推进 `0013`。中断或失败后的下一次离线命令 MUST 恢复到全旧状态或幂等继续到全新状态，不得把部分重写标记为完成，也不得把 manifest/inventory 外的未知历史文件声称为已迁移。

#### Scenario: Legacy local evidence 与 SQLite 一致升级
- **WHEN** local state 同时包含可唯一关联 run lineage 的 SQLite rows、nullable event JSONL 和 eval score JSONL
- **THEN** upgrader 为所有 run-scoped records 写入同一确定性 canonical trace，保留 event_id/seq/payload/score 与逐文件原始备份，并在 journal 标记完成后才开放新写入

#### Scenario: JSONL 中断后恢复
- **WHEN** upgrader 在临时文件写入、数据库 migration 或 atomic rename 任一步中断
- **THEN** 下一次启动根据 journal 与备份恢复或继续，不丢失 evidence、不留下混合完成状态

#### Scenario: 未登记 legacy 路径阻止普通入口升级
- **WHEN** 普通 run/eval/API/worker 入口发现旧 schema、未完成 journal 或当前配置路径未进入已迁移 manifest
- **THEN** 入口在写入前 fail closed，并要求使用离线命令选择 profile、提交 state-dir 与 legacy path inventory，不自动迁移或声称任意目录已被完整扫描；credential 只从环境或受信 `_FILE` 读取

#### Scenario: File-only 模式不能伪造 run lineage
- **WHEN** 调用方显式选择 `--file-only`，且 inventory 中存在显式 `record_scope=run`、普通 record 的真实非空 `run_id`，或 legacy ordinary telemetry 的非空 `payload.telemetry.context.run_id`
- **THEN** 离线命令拒绝迁移该 bundle，直到改用能经 typed settings 解析 root lineage 的 `--profile` 模式；纯非 run records 可保持原样登记新格式

#### Scenario: Legacy ordinary telemetry 的合成 run_id 不等于真实归属
- **WHEN** legacy TelemetryFacade JSONL 的 envelope `run_id` 是 context trace id 或字面值 `"telemetry"`，且 `payload.telemetry.context.run_id` 为空
- **THEN** upgrader 将其判为 non-run evidence，`--file-only` 可保持原样迁移；只有 nested context run_id 非空时才要求 DSN lineage backfill
