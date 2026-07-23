## MODIFIED Requirements

### Requirement: 本地 uv 兼容范围与发布基线分离
根 `[tool.uv].required-version` 与 release wrapper SHALL 接受 `>=0.11.29,<0.12`；GitHub 与 GitLab 当前执行环境 MUST 继续选择具体 `0.11.29` 及受审 OCI digest。`status: release` 的 preview、正式 build 和 publish plan MUST 记录各自实际使用的范围内 uv 版本；`status: no-release` 的 preview MUST 记录 `uv_version: null`，且不得仅为填充该字段启动 uv 或 build。具体 CI 版本不得被解释为唯一兼容 patch。

#### Scenario: 支持范围内 patch 可执行项目与发布入口
- **WHEN** 维护者使用 uv `0.11.29` 或更高且低于 `0.12` 的 patch 执行 lock check、frozen sync、build 或 release wrapper
- **THEN** 命令通过版本门禁，且对应发布 artifact 记录实际 uv 版本

#### Scenario: no-release 不伪造 uv 执行身份
- **WHEN** preview 判定没有 releasable commit
- **THEN** manifest 写入 `uv_version: null`，且 producer 不调用版本解析器、不启动任何 uv 子进程也不进入 build，consumer 只接受 `null`

#### Scenario: 支持范围外工具在副作用前被拒绝
- **WHEN** uv 版本低于 `0.11.29`、达到 `0.12` 或版本输出无法解析
- **THEN** 项目或 release wrapper 在 build、publish relay 与外部网络副作用前 fail closed

#### Scenario: 具体 CI pin 不收窄兼容范围
- **WHEN** GitHub setup 与 GitLab OCI image 选择具体 uv `0.11.29`
- **THEN** CI 运行保持可复现，但 release wrapper 仍接受其他满足 `>=0.11.29,<0.12` 的 patch
