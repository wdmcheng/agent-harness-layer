## ADDED Requirements

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
`make smoke-service` SHALL 通过真实 HTTP RUN-001 创建 run，并从共享 detail/events seam 观察独立 worker执行同一 `run_id`。worker pickup前，smoke MUST 从隔离 Redis stream entry逐值核对 `request_id`、effective `idempotency_key`、`tenant_id`、`run_id` 与 API/PostgreSQL证据一致。smoke MUST 输出 migration、Redis、API/auth、worker、fenced receipt、DBOS workflow、PostgreSQL event/checkpoint与终态关联证据；local、in-memory、日志推断或 mock结果不得替代这些证据。

#### Scenario: 暂停 worker 后提交再恢复
- **WHEN** smoke 暂停或尚未启动 worker、调用 API 创建 run，再启动 worker
- **THEN** API先返回 `status=created` run与 `run.queued` evidence，随后同一 run被 worker执行并产出可通过 HTTP读取的 PostgreSQL event stream与唯一终态

#### Scenario: Worker crash 后真实 reclaim 无重复 run
- **WHEN** smoke让 worker A pickup但在应用结果持久化与 ack前确定性退出，等待 idle lease后启动 worker B通过 `XAUTOCLAIM` reclaim，再以同一 idempotency key重复 RUN-001
- **THEN** Redis显示同一 stream entry ownership/delivery count变化，旧 receipt ack被拒绝，B复用同一 DBOS workflow/application run并完成；所有 HTTP/DBOS/event证据指向同一 `run_id`，数据库只有一条逻辑 run且 terminal唯一

#### Scenario: Queue 四字段逐值保持
- **WHEN** smoke比较 RUN-001请求/响应、PostgreSQL execution context、Redis entry、worker receipt和最终 CanonicalEvent
- **THEN** `request_id`、effective `idempotency_key`、`tenant_id`、`run_id` 在所有适用边界逐值一致，重试 attempt id不改写首次 queue correlation

#### Scenario: DBOS owner 建立后 hard crash 再恢复
- **WHEN** singleton worker A以稳定 executor id持久化 initial workflow/application owner/ref并进入 PENDING/durable状态，但在结果/terminal/ack前 hard-exit
- **THEN** smoke确认 A完全退出后才启动 B；B复用同 executor id、reclaim同 entry并让 DBOS恢复同 workflow/durable step，逐值证明 executor/workflow/owner refs与 crash前一致且不是首次启动

#### Scenario: Shared application checkpoint 经审批恢复
- **WHEN** smoke通过真实 HTTP为 `examples.dev_assistant`提交确定性需要审批的动作，worker执行到 waiting并写 PostgreSQL checkpoint/approval/event，随后 reviewer通过 APR-002 approve
- **THEN** API可读取同一 `run_id` waiting/checkpoint evidence；approve operation由独立 worker恢复原 continuation并完成，checkpoint、resolution lease、DBOS operation和唯一 terminal均来自共享 PostgreSQL/Redis链路

#### Scenario: Service deny 不创建 continuation
- **WHEN** smoke对另一条确定性 waiting run提交 deny
- **THEN** denied event/audit与 failed/fallback run原子收口，Redis中没有该 approval continuation operation，DBOS workflow和 tool handler计数为零

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
