## 背景

仓库当前已经提交 Product-Spec.md 和 DEV-PLAN.md 作为上游真相源，也有 OpenSpec schema/templates，但还没有活跃 baseline specs。DEV-PLAN Phase 1 是第一步实现：建立 uv workspace、可构建的核心包、service-app template shell、最低质量命令，以及合规/文档入口。

本变更刻意先创建脚手架和公共 seam，再进入运行时行为。后续 changes 会在这些路径和命令之上继续添加 configuration、storage、runtime、policy、tools、retrieval、observability、eval、examples、CI 和 release automation。

主要干系人：
- 需要可复制 service-app 形态的 agent 应用开发者。
- 需要稳定包边界和质量命令表面的脚手架维护者。
- 需要稳定任务路径和验证路径的后续 OpenSpec changes。

## 目标 / 非目标

**目标：**
- 建立根目录 uv workspace 和 lockfile 工作流。
- 让 `packages/agent-harness` 可构建为 wheel/sdist。
- 预留 `templates/service-app` 后端应用布局，但不实现业务 runtime。
- 提供带 Phase 1 行为的根目录 quality、test、smoke、build 和 license-check 命令。
- 添加 README、LICENSE、NOTICE 和 pre-commit 入口，把边界写清楚。

**非目标：**
- 不实现任何 durable runtime、DBOS adapter、storage schema、policy engine、HITL、tool execution、retrieval、observability provider、eval runner、API route behavior 或 service-profile Docker orchestration。
- 不实现四个 P0 example agents；除目录或空 package marker 外不添加行为。
- 不添加产品 UI 或 SaaS 管理表面。
- 不 vendor 上游 SDK 源码。

## 决策

1. 使用 uv workspace 作为唯一 Python 依赖和 workspace 管理器。
   - 理由：Product-Spec.md 要求 uv workspace，DEV-PLAN.md 也固定 uv 作为包管理器。单一依赖管理器能避免 lockfile 和脚本漂移。
   - 备选方案：Poetry 或普通 pip requirements。拒绝原因是它们会违背上游计划并引入第二套 workflow。

2. 本变更只把 `packages/agent-harness` 作为可构建核心包。
   - 理由：产品在功能模块前需要包边界。带版本导出的最小包已经足够完成 Phase 1 验证。
   - 备选方案：立即创建所有未来模块目录。拒绝原因是空的深层目录会制造虚假的完成感，并让后续 changes 更难审查。

3. 保持 `templates/service-app` 为 shell，而不是可运行的 agent 产品。
   - 理由：Phase 1 必须预留 app、CLI、worker、configs、eval、tests 和 docs 表面，但实际 agent runtime 属于后续 changes。
   - 备选方案：现在实现完整 FastAPI route 或 fake agent run。拒绝原因是这会把 Phase 5 和 Phase 12 的关注点提前塞进 workspace bootstrap。

4. 尽早定义质量命令，即使部分命令只验证 Phase 1 seams。
   - 理由：后续 changes 需要稳定命令名：`make quality`、`make test`、`make smoke-local`、`make build` 和 `make license-check`。
   - 备选方案：等各子系统存在后再添加命令。拒绝原因是这会延迟 dev-builder 和 CI 所需的契约。

5. 使用 import boundary checks 作为 Phase 1 契约。
   - 理由：Product Spec 反复依赖上游隔离。一个简单脚本就能强制核心包不依赖 templates/examples，并保证早期 template 代码不绕过包边界。
   - 备选方案：只依赖 code review。拒绝原因是这个边界是机械规则，应该由命令检查。

6. 立即添加 Apache-2.0 license 和 NOTICE。
   - 理由：合规是 P0 产品要求，并且从第一次提交起就影响 dependency 和 vendoring 决策。
   - 备选方案：接近 release 时再添加 license artifacts。拒绝原因是这会让早期复制片段和依赖决策长期未审计。

## 受影响表面

- 根目录项目文件：`pyproject.toml`、`uv.lock`、`Makefile`、`.pre-commit-config.yaml`、`README.md`、`LICENSE`、`NOTICE`。
- 核心包：`packages/agent-harness/pyproject.toml`、`packages/agent-harness/src/agent_harness/__init__.py`、包 metadata 和 tests。
- Template shell：`templates/service-app/pyproject.toml`、`templates/service-app/Makefile`、`.env.example`、README，以及预留的 `app/`、`agents/`、`configs/profiles/`、`eval-cases/`、`tests/` 和 `docs/` 路径。
- Scripts：import-boundary check、license check，以及根目录命令所需的任何最小 smoke helper。
- Tests：Phase 1 tests 只覆盖包 import/build metadata、workspace shape、command wiring 和 boundary checks。
- APIs：没有 runtime HTTP API 行为。
- 数据模型和迁移：无。
- 发布关注点：包含可构建 wheel/sdist；不包含 release automation。

## 测试 seam

- 在仓库根目录运行 `uv sync` 能解析 workspace。
- `uv build --package agent-harness` 产出 wheel/sdist artifacts。
- 在 workspace 或已安装 build artifact 中运行 `python -c "import agent_harness; print(agent_harness.__version__)"` 成功。
- `make quality` 针对 Phase 1 文件运行 ruff、pyright 和 import-boundary checks。
- `make test` 运行 Phase 1 unit/contract tests。
- `make smoke-local` 在不需要外部模型 key 或 SaaS providers 的情况下验证 workspace 和 service-app shell。
- `make license-check` 验证 `LICENSE`、`NOTICE` 和无未声明 vendored source baseline。
- README 检查覆盖目录职责和禁止的依赖方向。

## 风险 / 取舍

- [Risk] Scaffolding 可能看起来已经完整，但行为仍然不存在。-> Mitigation：README、proposal、specs 和 tasks 必须声明 runtime/storage/policy/eval 是本变更的非目标。
- [Risk] 如果质量命令只检查空文件，约束可能过弱。-> Mitigation：加入 boundary、import、package build 和 smoke checks，验证 public seams 而不是 private implementation。
- [Risk] 现在添加所有未来依赖会增加 lockfile churn，并遮蔽 Phase 1 范围。-> Mitigation：运行时依赖暂不进入，除非只是 package metadata groups 需要；各子系统依赖在自己的 changes 中添加。
- [Risk] 预留目录可能变成死结构。-> Mitigation：只创建 Product-Spec.md 要求的目录，并在 README 中说明后续 changes 拥有行为。
- [Risk] 在真实依赖存在前，license check 可能过度扩张。-> Mitigation：先从 Apache-2.0 与 NOTICE 存在性检查、vendoring detection 开始；在 release/compliance changes 中扩展 dependency license auditing。

## 迁移计划

这是第一个 baseline change，没有数据迁移。

实现路径：
- 创建 workspace 和 package/template shell 文件。
- 添加根目录 command wrappers 和 Phase 1 validation scripts。
- 运行 `uv sync`、`make quality`、`make test`、`make smoke-local`、`make build` 和 `make license-check`。
- 保持 Product-Spec.md 和 DEV-PLAN.md 不变，除非实现暴露出真实上游需求冲突。

回滚策略：
- 如果 workspace bootstrap 阻塞本地开发，回滚 change files 并删除生成的 lock/build artifacts。
- 因为没有引入 data schema 或 runtime state，回滚只发生在文件层。

## 待确认问题

- 在 release automation 存在前，根 package version 应该从 `0.1.0` 还是 `0.0.0` 开始？
- pyright 后续在 CI 中应该通过 Python `pyright` package、npm-managed binary，还是两者都跑？
- Phase 1 是否应包含所有 locked packages 的 dependency license scanning，还是把完整 dependency license audit 留给 release/compliance change？
