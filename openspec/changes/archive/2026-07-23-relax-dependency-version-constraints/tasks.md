## 1. 失败合同与解析基线

- [x] 1.1 冻结变更前 `uv.lock` 的全部 `(name, version, source)` identity，并建立可重复比较的本地证据：读取 `git show HEAD:uv.lock`，将每项规范化为 `name<TAB>version<TAB>json.dumps(source, sort_keys=True, separators=(",", ":"))`，按三元组排序后以换行连接且末尾不加换行，再计算 UTF-8 字节的 SHA-256。原始输出为 `count=207`、`sha256=bb9046c25267f611007c6b74ee74c3ff8e55f885b3f92d091aed0642c5adef58`，命令退出码 `0`。
  - 复算命令：`python3 -c 'import hashlib,json,subprocess,tomllib; d=tomllib.loads(subprocess.check_output(["git","show","HEAD:uv.lock"]).decode()); rows=sorted((p["name"],p["version"],json.dumps(p.get("source",{}),sort_keys=True,separators=(",",":"))) for p in d["package"]); payload="\n".join("\t".join(row) for row in rows).encode(); print(f"count={len(rows)}"); print(f"sha256={hashlib.sha256(payload).hexdigest()}")'`。
- [x] 1.2 新增 dependency policy 与 backend identity 合同；首次在旧 exact metadata/uv gate/旧 promotion/旧隔离构建上运行定向集合，退出码 `1`、`7 failed`，失败点与预期旧行为一致。
- [x] 1.3 先更新 dependency policy、workspace、template、release preview/promotion 合同预期，覆盖外部 metadata 的有界范围和根/模板 `0.2.0 -> ==0.2.0` 精确自依赖 promotion seam；2026-07-23 在当前错误范围实现上运行三个 metadata 节点和直接 `update_release_files` seam，均取得只由错误范围触发的新 red。旧 green 未复用，完整 promotion 的 backend 提前失败只归入 1.4。
  - Red 命令：`UV="$PWD/.artifacts/tools/uv-0.11.29/bin/uv" .venv/bin/pytest -q tests/contracts/test_dependency_version_policy_contracts.py::test_all_python_dependency_declarations_use_reviewed_compatible_ranges tests/contracts/test_workspace_packaging_contract.py::test_service_app_declares_core_dependency_without_member_only_workspace_source tests/contracts/test_release_preview_contracts.py::test_template_dependency_matches_project_version_while_root_keeps_workspace_override tests/contracts/test_release_promotion_contracts.py::test_update_release_files_keeps_workspace_self_dependencies_exact`。
  - 原始失败摘要：前三项均为 `assert 'agent-harness==0.1.0'` 对当前 `agent-harness>=0.1.0,<0.2` 失败；直接 promotion seam 在 `assert 'agent-harness==0.2.0'` 对旧实现生成的 `agent-harness>=0.2.0,<0.3` 失败；pytest 汇总 `4 failed`，命令退出码 `1`。
- [x] 1.4 为 preview 与正式 tag build 增加 build backend identity red 合同，证明旧正式隔离构建不会继承 lock，且 manifest 尚不能拒绝 Hatchling 缺失或漂移。
- [x] 1.5 增加 workspace 外默认隔离构建合同：复制核心 package 或解包 sdist、移除 workspace source、不预装 backend 且不使用 `--no-build-isolation`，证明兼容 build-system metadata 的真实消费者 seam。
- [x] 1.6 增加 dependency-group 冲突合同：`release` 与 `license` 必须分别 frozen sync 并显式排除另一组，拒绝重新引入无排除条件的 `--all-groups` 验收。

## 2. 兼容声明与发布保真

- [x] 2.1 将根 workspace 中可放宽的外部普通、dev、license、release、build 依赖和 `[tool.uv].required-version` 改为设计规定的有界范围；`agent-harness` 自依赖精确匹配当前项目版本，并保留 CI/release exact `0.11.29` 边界。
- [x] 2.2 将核心包 runtime、optional extra 和 build-system 依赖改为有界范围，保留 MCP `<2` 与 OpenTelemetry `<1.43` 等已知约束。
- [x] 2.3 将 service-app 模板中可放宽的外部 runtime、dev 和 build-system 依赖改为有界范围；`agent-harness` 自依赖精确匹配当前项目版本。
- [x] 2.4 修改 `scripts/release_workspace_contract.py`，使根与模板 promotion 统一精确匹配完整项目版本，并通过隔离 fixture 合同。
- [x] 2.5 修改 preview/formal build seam：以 `--group release --no-group license` frozen sync、核对 lock/环境内精确 Hatchling、以 `--no-build-isolation` 构建，并让两类 manifest 记录且 consumer 校验 backend identity。

## 3. Lock、文档与长期证据

- [x] 3.1 使用固定 uv 在不带 `--upgrade` 的情况下刷新 `uv.lock` metadata，证明 package identity 与 1.1 基线完全一致。
- [x] 3.2 同步根与模板双语 README、双语 release process，明确支持范围、当前 lock、CI/release exact 基线以及普通验证和显式升级边界。
- [x] 3.3 将 REQ-023 与 AC-069/070/071/072 映射到具体生产文件、精确 pytest node、双 CI producer 和 evidence path，并更新 acceptance validator 合同；AC-072 必须映射 preview/formal build backend identity 行为节点。

## 4. 定向验证

- [x] 4.1 运行 dependency policy、workspace/template、workspace 外默认隔离构建、release preview/promotion/formal build backend、documentation 和 acceptance matrix 精确合同并全部通过。
- [x] 4.2 分别以系统 uv `0.11.19` 和仓库固定 uv `0.11.29` 运行 lock check；固定 uv 分别执行 release/license 冲突感知 frozen sync，再完成无隔离精确 backend build、release dry-run 与 license check。

## 5. 全量验证与候选状态

- [x] 5.1 运行 ruff format/check、pyright、import boundary、全量 unit/contract/integration/eval/local smoke，共享依赖配置受影响时运行真实 service smoke。
- [x] 5.2 运行 OpenSpec change strict、全仓 strict、pre-commit 和 `git diff --check`，记录本地通过与 hosted-unverified 边界。
- [x] 5.3 在最终 review 前更新 Product Spec AC、DEV-PLAN Phase 16 和本 change tasks 为实际证据状态，冻结候选 diff；不执行 archive 或外部副作用。
  - 最终审查发现正式构建输出根若为 symlink，旧实现会在 `resolve()` 后丢失边界身份并可能越界清理。已先以 `test_formal_build_rejects_symlinked_release_root_before_cleanup` 取得退出码 `1` 的精确 red，再改为词法定界、逐段拒绝 symlink 且每次递归删除前重新校验；该节点和 release preview/promotion/formal build 相关合同、quality、release dry-run 均重新通过。
