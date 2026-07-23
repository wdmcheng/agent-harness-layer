## MODIFIED Requirements

### Requirement: 文档版本、链接与决策可复核
系统 SHALL 使文档中的内部路径、锚点、外部引用和技术版本可从当前 checkout 复核。Python dependency 的支持范围、当前精确解析与 CI/release 工具基线 MUST 分别与对应 `pyproject.toml`、`uv.lock` 和 CI/release 配置一致；Docker runtime MUST 按 Compose image reference 描述 pin 粒度；仓库未锁定的外部 CLI MUST 标明未锁定并记录本次验证版本，不得冒充 lock 内容。vendor isolation 与 Redis runtime/license policy MUST 由独立 ADR 记录背景、决策、替代方案、后果和复审触发条件。

#### Scenario: 当前 checkout 执行文档核验
- **WHEN** 维护者对当前维护文档运行链接、路径、命令与版本核验
- **THEN** 所有内部目标存在、文档命令可执行或明确标注 service 前置，dependency 支持范围、lock 解析、CI/release 基线、Compose 与外部 CLI 分别按其权威来源核验，已知上游表述冲突被纠正，外部事实有官方来源

#### Scenario: 外部 provider 或 Redis 版本变化
- **WHEN** 维护者计划升级 vendor SDK、Redis runtime 或改变部署用途
- **THEN** 其能从 ADR 找到必须保持的隔离边界、license/NOTICE 复审条件和需要重新验证的证据

#### Scenario: 维护者区分放宽与升级
- **WHEN** 维护者阅读根或模板 README 与 release process 的依赖维护章节
- **THEN** 文档明确说明放宽 `pyproject.toml` 不会自动升级 `uv.lock`，并给出普通 locked/frozen 验证与显式 upgrade 的不同受审边界
