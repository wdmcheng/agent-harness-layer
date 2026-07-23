## Context

仓库当前由三份 `pyproject.toml` 声明 workspace、核心包和 service-app 模板依赖，`uv.lock` 保存统一解析。多数声明使用 `==`，根 `[tool.uv].required-version` 也精确限制为 `0.11.29`；与此同时 CI、release wrapper、GitLab OCI image 和合规证据确实需要不可变版本。发布 promotion 还会主动把 `agent-harness` 自依赖写回 exact 或 `==MAJOR.MINOR.*`，因此一次性手改声明无法长期成立。

## Goals / Non-Goals

**Goals:**
- 用有界 PEP 440 范围表达支持窗口，用 `uv.lock` 表达当前精确解析。
- 让系统已安装并验证过的 uv `0.11.19` 与 CI 基线 `0.11.29` 都能使用当前项目。
- 保持 CI/release 的 `0.11.29` 精确证据和所有已锁 package identity 不变。
- 让 promotion、合同测试和文档长期维持该分层。

**Non-Goals:**
- 不选择新依赖版本，不运行 `uv lock --upgrade`。
- 不放宽容器 digest、CI action/image、release executable 校验或合规快照。
- 不改变 runtime/API/数据库/部署行为。

## Decisions

1. **声明采用显式下界和上界。** 稳定版本使用 `>=current,<next-major`；`0.x` 使用 `>=current,<next-minor`；CalVer 工具限制在下一年度；已知 Logfire/OpenTelemetry 组合保留 `<1.43`。相比仅写 `>=`，有界范围不会静默跨过已知破坏性边界；相比 `~=`，显式上界更容易审查且能表达特殊组合限制。
2. **lock 是唯一精确 Python 解析。** 变更前后比较 `uv.lock` 每个 package 的 `(name, version, source)`；刷新只允许 metadata 变化。普通开发、CI 和 release 使用 `--locked`/`--frozen`，升级另开受审变更并显式使用 `--upgrade`。
3. **uv 本地范围与发布基线分离。** 根 `required-version` 使用 `>=0.11.19,<0.12`，覆盖当前系统已验证旧 patch；GitHub、GitLab 与 release wrapper 继续精确要求 `0.11.29`。不开放到 `0.12`，避免跨 minor 的命令、lock 或发布语义变化。
4. **promotion 统一生成精确自依赖。** `agent-harness` 是当前 workspace 自身发布的核心包，不是可独立漂移的第三方依赖；根 workspace 和模板都精确匹配完整新版本，例如 `0.2.0 -> ==0.2.0`。这避免模板与核心包跨版本组合，并修复旧逻辑中根 exact、模板通配的分叉。
5. **exact pin 只保留在明确的身份耦合边界。** 同仓库自依赖、GitHub setup、GitLab image+digest、release wrapper、容器 image+digest、license snapshot 可以继续 exact；三份 `pyproject.toml` 的外部依赖本轮不保留无说明 exact pin。
6. **发布构建显式绑定 lock 内 backend。** 默认 build isolation 会单独解析 `[build-system]`，不会继承项目 lock，因此 preview 与正式 tag checkout 都先执行 `uv sync --frozen --group release --no-group license`、核对 lock/环境中的 Hatchling identity，再用 `uv build --no-build-isolation`。preview 与 `release-build/v1` manifest 都记录 backend 名称和版本，consumer 对缺失或漂移 fail closed。选择该方案而不是额外 build constraint，是因为 release group 已有受审精确 lock；`license` group 通过独立的 `uv sync --frozen --group license --no-group release` 验证，不尝试同时选择两组互斥的 Rich 解析。默认隔离解析只保留给 workspace 外消费者验证兼容 metadata。

## Affected Surfaces

- 配置：三份 `pyproject.toml` 与 `uv.lock` metadata。
- 发布：`scripts/release_workspace_contract.py` 的自依赖重写，preview/formal build 的 frozen backend 准备、无隔离构建和 manifest identity。
- 合同：dependency policy、workspace/template、release preview/promotion、文档和 acceptance matrix。
- 文档：根/模板双语 README、双语 release process、Product Spec、DEV-PLAN。
- 不涉及 API、数据模型、migration、UI 或外部系统。

## Testing Seams

- 从三份 `pyproject.toml` 的公开 package metadata 检查每个可放宽的外部 requirement 均有下界和兼容上界；两处 `agent-harness` 自依赖精确等于对应项目版本，且无其他 `==`。
- 对变更前后 `uv.lock` package identity 做确定性比较，并运行 `uv lock --check` 与 frozen sync。
- 用系统 uv `0.11.19` 检查 lock，用仓库固定 uv `0.11.29` 执行完整 CI/release 合同。
- 通过公开 `update_release_files` seam 在隔离 fixture 中模拟 `0.2.0` promotion，检查根和模板都精确匹配 `agent-harness==0.2.0`。
- 在 preview 与正式 tag-build fixture 中验证 frozen sync 后只使用 lock 内 `hatchling 1.30.1`，manifest 记录精确 backend，缺失/漂移在产物授权前失败。
- 通过文档合同检查支持范围、lock 解析和 CI/release exact 基线均被明确说明。

## Risks / Trade-offs

- [范围过宽时上游兼容声明不可靠] → `0.x` 只开放下一次版本前，稳定包限制下一主版本；真实升级仍需单独测试与审查。
- [放宽 metadata 意外触发重新解析] → 禁止 `--upgrade`，冻结并比较全部 package identity，任何版本或 source 变化均失败。
- [无条件 `--all-groups` 同时选择互斥 release/license 组] → 两组分别 frozen sync 并显式排除另一组，合同拒绝重新引入不可满足的 all-groups 门禁。
- [旧 uv patch 行为差异] → 下界只放到本机已有验证入口 `0.11.19`，不接受更早 patch；CI/release 继续精确 `0.11.29`。
- [默认构建隔离会在范围内选择更新版 Hatchling] → 仓库 preview/formal build 先 frozen sync 并核对 backend，再以 `--no-build-isolation` 构建和记录 identity；workspace 外消费者验证保留默认隔离以证明兼容 metadata。
- [promotion 正则误改其他 dependency] → 在隔离 fixture 中同时验证根、模板和无关依赖保持不变。

## Migration Plan

1. 冻结当前 lock package identity，并先建立会在旧 exact 声明和旧 promotion 行为上失败的合同。
2. 放宽声明和 `required-version`，修改 promotion，刷新 lock metadata但不升级。
3. 运行 targeted contracts、lock/frozen sync、质量、全量测试、构建、release/license 与 local/service smoke。
4. 在最终 review 前同步验收状态并冻结 diff；审查后不做实质修改。
5. 回滚时可恢复三份声明、promotion 和 lock metadata；无数据 migration 或外部副作用。

## Open Questions

无。后续是否实际升级到范围内新版本必须另开受审变更，不属于本次工作。
