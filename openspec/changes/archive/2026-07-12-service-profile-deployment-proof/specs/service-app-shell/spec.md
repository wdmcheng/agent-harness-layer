## ADDED Requirements

### Requirement: Service smoke 使用真实独立 API/worker
service-app的 `smoke-service` SHALL在仓库内和 workspace外复制项目中启动真实四服务，并分别证明：(1) initial DBOS owner/workflow已持久化后 hard crash -> Redis reclaim ->同 workflow恢复；(2) `examples.dev_assistant`产生 application waiting checkpoint，APR-002 approve经 worker恢复、deny零 continuation。脚本 MUST使用有效 service credential、共享 PostgreSQL/Redis，不得用 direct Python worker、DBOS metadata冒充 application checkpoint、共享 JSONL或日志推断替代。

#### Scenario: Workspace 外模板保留四服务证明
- **WHEN** smoke 把模板复制到 workspace 外，只安装已构建的核心 wheel 并运行 `make smoke-service`
- **THEN** 四服务使用复制项目自身的 Compose、profile 和脚本完成同一真实链路，不依赖仓库源码路径、根 `PYTHONPATH` 或 in-process fake

#### Scenario: Smoke 失败保留可操作诊断
- **WHEN** migration、Redis、API readiness、worker pickup、DBOS execution 或 event读取任一环节失败
- **THEN** smoke 以非零退出并指出失败边界与脱敏关联 id，不输出 DSN password、token、绝对敏感路径或 provider raw error

#### Scenario: Workspace 外 smoke 保留认证与资源隔离
- **WHEN** 复制项目运行四服务 smoke并在中途失败
- **THEN** service verifier仍拒绝缺失/无效凭据、有效临时凭据只在本轮生效，默认 cleanup删除复制项目本轮 containers/network/volume/queue/credential且不触碰仓库或其他项目资源
