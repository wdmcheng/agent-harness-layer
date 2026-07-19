# service-deployment-boundaries Specification

## Purpose
定义 service profile 的 PostgreSQL、Redis、API、runtime worker 四服务部署边界，以及真实 HTTP 到 worker 的恢复性 smoke、认证隔离、关联证据和资源清理契约。
## Requirements
### Requirement: Service profile 以四个独立服务协作
模板 Docker Compose service profile SHALL 启动 PostgreSQL、Redis、API 和 runtime worker。API/worker MUST 使用同一 storage DSN、queue DSN、profile、DBOS system database 与 PostgreSQL CanonicalEvent sink；API 停止执行 executor，worker 不暴露 HTTP 管理面。smoke MUST 通过真实 service API-key/Bearer verifier，凭据只由隔离 smoke 进程生成并以环境变量传递，不写入仓库、镜像、profile、日志或 artifact。

#### Scenario: Compose 四服务达到可用状态
- **WHEN** 开发者执行 `make smoke-service`
- **THEN** PostgreSQL、Redis、API 和 worker 均在同一模板 compose project 中启动并通过各自 readiness，迁移在请求提交前达到 head

#### Scenario: API 与 worker 配置一致
- **WHEN** contract 检查两个容器的环境、profile 和 command
- **THEN** 它们指向相同 PostgreSQL/Redis/DBOS/PostgreSQL-event配置，且只由各自进程承担 API 或 worker职责

#### Scenario: Service verifier 拒绝缺失或无效凭据
- **WHEN** smoke 在 worker停止时分别以缺失、无效和有效隔离凭据调用 RUN-001
- **THEN** 前两次返回稳定 401 且 run/queue/audit无创建副作用；有效凭据注入原 IdentityContext并成功创建 `status=created` run

### Requirement: Service smoke 证明真实 HTTP 到 worker 链路
`make smoke-service` SHALL 通过真实 HTTP RUN-001 创建 run，并从共享 detail/events seam 观察独立 worker 执行同一 `run_id`。worker pickup 前，smoke MUST 从隔离 Redis stream entry 逐值核对 `request_id`、effective `idempotency_key`、`tenant_id`、`run_id` 与 API/PostgreSQL 证据一致。smoke MUST 输出 migration、Redis、API/auth、worker、fenced receipt、DBOS workflow、PostgreSQL event/checkpoint、usage、approval resolution 与终态关联证据；local、in-memory、日志推断或 mock 结果不得替代这些证据。approval approve/deny 场景 MUST 注入 outbox sink 写前失败、写后 ack 前失败与进程重启，证明公开状态只在唯一 `approval.resolved` 和对应 terminal 依序持久化后收口，恢复不重放 provider、tool handler 或 continuation。

#### Scenario: 暂停 worker 后提交再恢复
- **WHEN** smoke 暂停或尚未启动 worker、调用 API 创建 run，再启动 worker
- **THEN** API 先返回 `status=created` run 与 `run.queued` evidence，随后同一 run 被 worker 执行并产出可通过 HTTP 读取的 PostgreSQL event stream 与唯一终态

#### Scenario: Worker crash 后真实 reclaim 无重复 run
- **WHEN** smoke 让 worker A pickup 但在应用结果持久化与 ack 前确定性退出，等待 idle lease 后启动 worker B 通过 `XAUTOCLAIM` reclaim，再以同一 idempotency key 重复 RUN-001
- **THEN** Redis 显示同一 stream entry ownership/delivery count 变化，旧 receipt ack 被拒绝，B 复用同一 DBOS workflow/application run 并完成；所有 HTTP/DBOS/event 证据指向同一 `run_id`，数据库只有一条逻辑 run 且 terminal 唯一

#### Scenario: Queue 四字段逐值保持
- **WHEN** smoke 比较 RUN-001 请求/响应、PostgreSQL execution context、Redis entry、worker receipt 和最终 CanonicalEvent
- **THEN** `request_id`、effective `idempotency_key`、`tenant_id`、`run_id` 在所有适用边界逐值一致，重试 attempt id 不改写首次 queue correlation

#### Scenario: DBOS owner 建立后 hard crash 再恢复
- **WHEN** singleton worker A 以稳定 executor id 持久化 initial workflow/application owner/ref 并进入 PENDING/durable 状态，但在结果/terminal/ack 前 hard-exit
- **THEN** smoke 确认 A 完全退出后才启动 B；B 复用同 executor id、reclaim 同 entry 并让 DBOS 恢复同 workflow/durable step，逐值证明 executor/workflow/owner refs 与 crash 前一致且不是首次启动

#### Scenario: Shared application checkpoint 经审批恢复
- **WHEN** smoke 通过真实 HTTP 为 `examples.dev_assistant` 提交确定性需要审批的动作，worker 执行到 waiting 并写 PostgreSQL checkpoint/approval/event，随后 reviewer 通过 APR-002 approve，并分别在 resolution outbox 写前、写后 ack 前注入故障与重启
- **THEN** API 可读取同一 `run_id` waiting/checkpoint evidence；approve operation 由独立 worker 恢复原 continuation且 tool handler 只执行一次，恢复只补投稳定 outbox；唯一 `approval.resolved` 先于唯一 terminal 持久化，两者确认后才公开 approved 与 run 终态

#### Scenario: Service deny 不创建 continuation
- **WHEN** smoke 对另一条确定性 waiting run 提交 deny，并分别在 denied resolution outbox 写前、写后 ack 前注入故障与重启
- **THEN** Redis 中没有该 approval continuation operation，DBOS workflow 和 tool handler 计数为零；公开状态保持 waiting，恢复仅补投稳定 outbox，直到唯一 denied resolution 与 failed/fallback terminal 依序持久化后才收口

### Requirement: 当前形态与未来拆分路径可审计
项目 SHALL 用架构说明、部署边界图和 ADR 明确当前 P0 形态与未来顺序：先拆 runtime worker，再拆 tool/model gateway，最后拆 observability/event pipeline；storage service 只有在 repository contract 稳定后才拆。所有未来边界 MUST 保持 DTO、CanonicalEvent、source/trust/context、guardrail/audit 和 correlation fields。

#### Scenario: 维护者可从文档指出所有边界
- **WHEN** 维护者阅读根/模板 README、部署架构图与 P0 service boundary ADR
- **THEN** 能区分 API、runtime worker、model/tool gateway、storage、event pipeline 的当前进程形态、所有权、交换 seam 和允许拆分顺序

#### Scenario: 规划能力不伪装为已实现
- **WHEN** 文档描述尚未物理拆分的 tool/model/storage/event 服务
- **THEN** 这些能力明确标记为未来边界，不被 Compose、health 或 README 声称为当前已部署服务

### Requirement: Smoke 默认清理隔离资源
service smoke SHALL 使用唯一 compose project/queue namespace和临时 credential，并在成功、失败或中断退出时清理 container、network、Redis stream/group/dedupe、临时 credential/env与临时文件。PostgreSQL named volume默认删除；只有调用方显式设置 `SERVICE_APP_KEEP_DATA=1` 时才可保留并输出 project/volume名和清理命令。

#### Scenario: 默认失败路径不留资源
- **WHEN** smoke 在 migration、auth、API、pickup、DBOS或event阶段失败
- **THEN** finally cleanup删除本轮 containers/network/volume/queue namespace和credential，不影响其他 compose project或用户 Docker资源

#### Scenario: 显式保留数据可审计
- **WHEN** 调用方设置 `SERVICE_APP_KEEP_DATA=1`
- **THEN** smoke仍清理 containers/network/credential和queue namespace，仅保留本轮 PostgreSQL volume，并输出后续复用与删除命令

### Requirement: Service profile 以只读 secret file 装配应用凭据
模板 Docker Compose SHALL 为 API、runtime worker 和 migration composition 提供一致的只读 secret mount 与 `<BASE_ENV>_FILE` 引用。真实 secret MUST 由调用方在隔离环境生成并在成功、失败或中断时清理；仓库、镜像、profile、Compose 输出、日志和 artifact MUST NOT 保存或回显 secret 值。P0 部署 MUST 直接消费 env/secret file，不得引入 `SecretProvider`、Vault 或 KMS adapter。

#### Scenario: API、worker 与 migration 消费同一只读引用
- **WHEN** `make smoke-service` 以临时 secret file 启动 service profile
- **THEN** API、worker 与 migration 使用同一 typed field 的 `_FILE` 引用和只读 mount，完成启动后公开 health/evidence 不包含 secret 原值

#### Scenario: 无效 secret 阻止服务进入可用状态
- **WHEN** mount 缺失、不可读、为空、越界、为 symlink 或与 direct value 冲突
- **THEN** 依赖该配置的应用进程在监听、连接或运行 migration 前失败，Compose readiness 不把该进程标为可用，诊断不泄漏 secret 或受信 root 外路径

#### Scenario: Smoke 清理临时 secret
- **WHEN** service smoke 成功、失败或被中断
- **THEN** cleanup 删除本轮临时 secret 文件及其引用环境，不影响其他 compose project，输出只包含安全资源标识和清理结果

### Requirement: Service smoke 证明共享预算跨进程一致性
Service profile SHALL 使用真实 PostgreSQL row lock/CAS 与 Redis worker delivery/reclaim 验证 shared parent ledger；SQLite/local evidence MUST 单独报告，不能替代真实 service 证据。

#### Scenario: PostgreSQL 与 Redis 混合竞争和恢复
- **WHEN** direct 与 delegation 对同一 parent 并发，并在 reservation、外部副作用和 settlement 各崩溃窗口触发 worker reclaim
- **THEN** service smoke 证明 token/cost 不超支、外部调用不重复、unknown 保守占用、child 不双计且最终 terminal 只在全部 claim 封闭后可见

#### Scenario: Service profile 隔离同 tenant 多 root
- **WHEN** 同一 tenant 的两个独立 root runs 与其中一个 root 的 delegation 跨 API/worker 进程并发执行
- **THEN** PostgreSQL 以各 root 自身非空 `budget_owner_run_id` 隔离两条 ledger，同时让同一 root 的 direct、delegation与child allocation命中同一 row lock/CAS；Redis reclaim不得改变owner或串用另一root余额

#### Scenario: Cost-disabled service recovery 不误封锁
- **WHEN** `max_cost_usd_per_run=null` 的普通model、embedding miss或delegated child在PostgreSQL/Redis流程返回可信token与合法`cost=null/cost_status=unavailable`并经历worker reclaim
- **THEN** service按token维度幂等settle、cost impact为0且不单独needs_review，terminal在其他claim封闭后可见；非法cost/status反例仍稳定拒绝

#### Scenario: Service fingerprint secret 复用 CFG-001
- **WHEN** Compose 通过只读 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE` 为 API 与 worker 注入同一 fingerprint key，并分别执行正常启动及 direct/file 冲突、symlink、超限、非 UTF-8 失败反例
- **THEN** 两个进程只经 typed settings 获得相同 key semantics，正常路径生成可重放 opaque fingerprint；失败路径在任何 run/provider/queue 副作用前结构化退出，Compose config、health、doctor、logs、PostgreSQL 与 artifacts 均不含原值
