## ADDED Requirements

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
