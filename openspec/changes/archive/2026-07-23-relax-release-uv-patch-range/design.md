## Context

仓库当前有三种不同性质的 uv 版本信息：`pyproject.toml` 的支持范围、GitHub/GitLab 选择的具体执行环境，以及 preview/build/publish artifact 记录的单次执行身份。现有实现把三者都收敛到常量 `0.11.29`，因此支持范围内的新 patch 会在 release wrapper 内被拒绝，manifest 也不能真实反映执行版本。用户已确认以 `0.11.29` 为安全下界、以 `0.12` 为破坏性上界。

## Goals / Non-Goals

**Goals:**

- release build 与 publish wrapper 接受 `>=0.11.29,<0.12`。
- `release` preview、正式 build 和 publish plan 记录实际 uv 版本，并由现有 checksum/approval 身份链绑定；`no-release` preview 记录 `uv_version: null`，不伪造未发生的工具执行身份。
- 低于下界、跨入 `0.12`、无法解析或 plan/execute 版本漂移时在构建或网络副作用前失败。
- 保留 GitHub/GitLab 当前具体 `0.11.29` 环境和 GitLab OCI digest 的可复现性。

**Non-Goals:**

- 不放宽 uv minor，不升级 Python package lock，不改变 build backend identity。
- 不要求 preview 与 tag 后正式 build 使用同一 patch；正式 build 会从 tag target 重建并形成自己的权威证据。
- 不执行真实外部发布，不改变 provider/registry 授权边界。

## Decisions

1. **一个规范范围，具体环境另行选择。** 共享发布合同定义 `>=0.11.29,<0.12`，根 `required-version` 和 wrapper 都使用该范围。GitHub setup 与 GitLab image 继续选择 `0.11.29`，因为 CI job 和 OCI image 必须解析为具体工具身份；该选择不再作为兼容性拒绝条件。相比让 setup action 接受浮动范围，这能避免同一 workflow 配置随时间漂移。
2. **解析真实 CLI 输出，不把支持下界写进 manifest。** wrapper 从 `uv --version` 的稳定前缀读取三段数字版本，返回 executable 与实际版本。preview、正式 build 和 publish plan 写入该实际版本。无法解析、低于 `0.11.29` 或达到 `0.12` 时 fail closed。相比只记录常量，这能让 artifact 证明本次真正执行的工具。
3. **范围校验与证据绑定分层。** preview/build consumer 先验证 `uv_version` 是规范三段版本且落在支持范围，再继续验证 schema、backend 和 artifact identity。registry plan 把本次 publish uv 版本放入动态 approval payload；execute 重新解析实际版本并与已批准 plan 比较，因此范围内版本可以使用，但审批后换 patch 仍属于身份漂移。
4. **不要求跨阶段 patch 相同。** preview 只提供无副作用预演，正式 build 从 tag target 重建并在 `release-build/v1` 记录自己的 uv；registry 只上传正式 build artifact。强制 preview 与 formal build patch 相同会重新形成不必要的 exact 耦合，且不会提高正式 artifact 的身份闭合度。
5. **下界选择基于安全基线而非 0.11.19 功能失败。** `0.11.29` 包含此前 patch 的 tar/ZIP 和 PEP 517 backend-path 安全加固；本轮把它设为最低受支持版本，但不宣称 `0.11.19` 在本项目功能上不兼容。
6. **preview 的工具身份随状态解释。** `status: release` 时 producer 必须在实际 build 前解析 uv，并把三段版本写入 `uv_version`；consumer 必须校验其落在支持范围。`status: no-release` 时 producer 必须写入 `uv_version: null`，且不得仅为填充证据启动 uv 或 build；consumer 只接受 `null`。这避免把“没有执行”伪装成某个工具版本，也保持 no-release 的无副作用边界。

## Affected Surfaces

- 配置与文档：根 `pyproject.toml`、Product Spec、DEV-PLAN、双语 README/release process、长期 OpenSpec delta。
- 发布共享合同：uv 版本解析、范围验证和 executable identity。
- 生产者与 consumer：release preview、正式 build、registry publish plan/execute。
- 合同测试：版本边界、实际版本写入、审批后 patch 漂移、具体 CI 环境与支持范围分离。
- 无 API、数据库、migration、UI 或 runtime service 变化。

## Testing Seams

- CLI seam：用真实 uv `0.11.29` 和本机 `0.11.31` 分别执行 `uv lock --check`、frozen release sync、无隔离 build 与 release dry-run。
- 发布模块 seam：构造 `uv --version` fixture，验证 `0.11.28`、`0.12.0` 和畸形输出被拒绝，`0.11.29`、`0.11.31` 被接受并返回实际版本。
- Artifact seam：`release` preview 与 `release-build/v1` 的 `uv_version` 等于实际 executable；consumer 接受范围内 patch 并拒绝范围外版本。`no-release` preview 固定为 `uv_version: null`，且合同通过替换公开版本解析 seam 证明解析器与任何 uv 子进程都不会被调用，也不会进入 build。
- Publish seam：loopback registry 替身验证范围内 uv 可进入上传合同；plan/execute uv patch 漂移在 relay 启动前失败。
- 配置 seam：GitHub/GitLab 仍精确选择 `0.11.29` 和既有 digest，同时依赖策略合同不再把该值解释为唯一兼容 patch。

## Risks / Trade-offs

- [patch 版本仍可能出现回归] → 保留受审下界、上界和真实版本证据；CI 固定环境继续稳定，升级 CI 具体版本单独审查。
- [仅做范围校验会让审批后工具漂移] → publish plan 把实际 uv 版本纳入动态 approval identity，execute 重新计算并严格比较。
- [解析厂商附加 build metadata 出错] → 只接受 `uv X.Y.Z` 前缀，允许其后存在 Homebrew/commit/platform说明，不接受非三段数字版本。
- [文档把具体 CI pin 再次误写为兼容下界] → 双语合同明确三层真相，并以维护文档测试扫描关键表述。

## Migration Plan

1. 先更新 Product Spec、DEV-PLAN 和 OpenSpec delta，形成 `>=0.11.29,<0.12` 的上游行为真相。
2. 先写范围、实际 identity 和 plan 漂移的失败合同，再修改共享 helper 与 producer/consumer。
3. 保持 `uv.lock` package identity 不变，分别以 uv `0.11.29`、`0.11.31` 验证受影响 release seam。
4. 回滚时恢复 wrapper exact 校验、manifest 固定值和对应文档；无数据迁移或外部副作用需要撤销。

## Open Questions

无。CI 具体版本何时从 `0.11.29` 升级不属于本轮。
