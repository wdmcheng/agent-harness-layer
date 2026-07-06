## 1. Workspace 和核心包

- [x] 1.1 创建根目录 `pyproject.toml` uv workspace，把 `packages/agent-harness` 和 `templates/service-app` 作为 members；用 `uv sync` 验证。
- [x] 1.2 创建 `packages/agent-harness/pyproject.toml`，包含 hatchling 构建元数据和 `agent-harness` 包名；验证元数据可通过 uv 看到。
- [x] 1.3 创建 `packages/agent-harness/src/agent_harness/__init__.py`，导出包版本；验证 `python -c "import agent_harness; print(agent_harness.__version__)"` 在 workspace 内可运行。
- [x] 1.4 配置 `uv build --package agent-harness` 产出 wheel 和 sdist artifacts；验证 build 命令成功。
- [x] 1.5 创建 workspace spec 要求的顶层 `packages/`、`templates/`、`examples/`、`docs/` 和 `scripts/` 边界；验证根目录布局存在。

## 2. Service-App Template Shell

- [x] 2.1 创建 `templates/service-app/pyproject.toml`，通过 workspace/path dependency 依赖 `agent-harness`；验证 `uv sync` 能解析，且不需要相对源码导入。
- [x] 2.2 在 `templates/service-app/app/{api,cli,workers}`、`agents/`、`configs/profiles/`、`eval-cases/{drafts,approved}`、`tests/` 和 `docs/` 下创建预留 template layout；验证路径存在。
- [x] 2.3 添加 `templates/service-app/configs/profiles/local.yaml`，提供后续 fake provider 和 local-jsonl integration 所需的最小 local-profile defaults；验证文件存在且可解析。
- [x] 2.4 添加 `templates/service-app/.env.example`，说明 local profile switch，以及 Phase 1 不需要真实 provider keys；验证 template smoke 不需要 secrets。
- [x] 2.5 添加 `templates/service-app/README.md`，包含 app developer 启动说明和 scaffold maintainer 边界说明；验证两个受众都被明确点名。
- [x] 2.6 添加 Phase 1 `templates/service-app/Makefile` 或根目录分发的 template smoke 命令；验证本地 smoke 命令不依赖外部服务也能完成。

## 3. 质量和合规入口

- [x] 3.1 添加根目录 `Makefile` 命令：`quality`、`test`、`smoke-local`、`build` 和 `license-check`；验证每个命令都能从仓库根目录调用。
- [x] 3.2 为 Phase 1 code surface 添加 ruff 和 pyright 配置；验证 `make quality` 同时运行两个工具。
- [x] 3.3 添加 `scripts/import_boundary_check.py`，拒绝核心包依赖 templates/examples，并阻止早期 vendor SDK leakage 越过允许的未来 adapter 边界；验证它在 `make quality` 中运行。
- [x] 3.4 添加 `scripts/license_check.py`，验证 Apache-2.0 `LICENSE`、`NOTICE` 和 undeclared vendored-source baseline；验证它在 `make license-check` 中运行。
- [x] 3.5 添加 `.pre-commit-config.yaml`，指向 Phase 1 quality checks；验证环境设置后 `pre-commit run --all-files` 可执行。
- [x] 3.6 添加 Apache-2.0 `LICENSE` 和根目录 `NOTICE`；验证 `make license-check` 通过。

## 4. 文档和验证证据

- [x] 4.1 添加根目录 `README.md`，覆盖脚手架目的、快速开始、项目结构、agent app developer 入口、scaffold maintainer 入口、license/compliance 和 release-process 状态；验证必需章节存在。
- [x] 4.2 在根 README 中记录禁止的依赖方向：core 不依赖 templates/examples，app entrypoints 不包含业务 agent 逻辑，vendor SDKs 留在未来 adapters 或受控集成模块后面；验证边界文本可搜索。
- [x] 4.3 为 package import、workspace 形态和 template shell 结构添加 Phase 1 unit 或 contract tests；验证 `make test` 通过。
- [x] 4.4 运行并记录 Phase 1 命令集：`uv sync`、`make quality`、`make test`、`make smoke-local`、`make build` 和 `make license-check`；在标记本 change complete 前，把命令结果作为实现证据。
