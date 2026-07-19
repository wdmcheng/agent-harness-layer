## Source Links

- Product-Spec.md：`REQ-004` 配置系统，`AC-008`、`AC-063`，以及 `OUT-010` 对 P0/P1 密钥能力的边界。
- DEV-PLAN.md：`Phase 13.6: 配置启动失败与 Docker Secret File`；依赖 `Phase 13.5`，并为 `Phase 13.7` 提供稳定配置边界。
- API-Contract.md：`CFG-001` typed settings 与 application startup failure 契约。
- 设计稿 / 架构图：`docs/architecture/agent-harness-deployment-boundaries.drawio` 的容器 secret mount、service profile 与应用启动边界；`docs/architecture/pydantic-ai-agent-architecture.drawio` 的配置与 redaction 信任边界。
- ADR：`docs/adr/0001-p0-service-boundaries.md` 的 API/worker/migration composition、credential 不落仓库/profile/log/artifact 与临时文件清理约束。

## Why

P0 已声明 service profile 可以消费只读 Docker secret file，但当前 loader 只合并 YAML、`.env`、进程环境变量和显式 overrides，应用入口也没有统一的 fail-closed 启动错误契约。必须先把 secret file 读取、启动失败和全观测面脱敏固定为可测试行为，才能把 Phase 1-13 作为可信部署基线。

## What Changes

- 在 typed settings 合并边界支持受控 `<BASE_ENV>_FILE` 输入，并把文件内容映射到对应 typed field；direct value 与 `_FILE` 同时存在时稳定失败，不静默选择优先级。
- 只允许读取显式受信 root 内的普通、非符号链接、绝对路径文件；目录、越界、不可读、空值、非 UTF-8 和超限内容全部 fail-closed。
- 统一 CLI、FastAPI、worker 与 migration composition 的缺失或无效配置失败，提供稳定 error code、field path 和不含 secret 的修复提示。
- 在 Docker Compose、service profile 和 `.env.example` 中提供只读 secret mount 与 `_FILE` 装配示例，不提交真实 secret。
- 对错误、doctor、health、日志、trace、eval、audit 和其他公开 evidence 执行统一 redaction，并增加 secret fixture 回归验证。

## Non-Goals

- 不引入 `SecretProvider` 抽象，不实现 Vault、云 KMS、secret rotation、远程 secret store 或运行时热更新；这些能力属于 P1。
- 不改变现有 typed settings 的字段模型，不新增业务 API，不实现 model usage、delegation 或 SSE。
- 不自动归档、发布、push 或推进 Phase 14/15。

## Capabilities

### New Capabilities

- 无。

### Modified Capabilities

- `typed-config`：增加 `_FILE` 到 typed field 的安全加载、冲突和稳定错误要求，并统一 application startup fail-closed 行为。
- `service-deployment-boundaries`：增加 service profile 的只读 Docker secret mount、部署装配与无泄漏启动验证要求。

## Impact

- 受影响代码：`packages/agent-harness/src/agent_harness/config/**`、`templates/service-app/app/main.py`、`templates/service-app/app/runtime.py`、`templates/service-app/app/workers/runtime_worker.py` 和 migration composition 入口。
- 受影响配置：`templates/service-app/docker-compose.yml`、`.env.example`、`configs/profiles/service.yaml`；只增加引用和装配，不保存 secret 值。
- 受影响测试：typed config contracts、CLI/API/worker/migration startup composition、wheel-only template、local/service smoke 与 secret grep。
- 不新增外部依赖或数据库 migration。
