## 1. 红灯与基线

- [x] 1.1 在 `tests/contracts/test_service_app_runtime_entrypoint_contracts.py` 建立 workspace 外 Make dry-run 红灯，逐值覆盖 local/service 的 profile、profiles-dir、`--once` 与可选 `ENV_FILE`。
- [x] 1.2 建立两个可独立定位的 workspace 外红灯：其一实际导入 copied app factory，在 clean/冲突 `.env` 下比较显式空 env file 的配置与健康结果；其二独立进入 copied runtime composition settings boundary，在 storage/provider 等副作用前逐值比较同一显式 env file 与配置结果。另以 worker 接线红灯覆盖两种启动模式的 env file 逐层传递。
- [x] 1.3 记录修复前精确失败断言与退出码，并确认测试不调用真实 provider、外部工具或 SaaS。

## 2. 最小实现

- [x] 2.1 只修改 `templates/service-app/Makefile`，修正 worker 参数接线并保持 service 常驻、非 service `--once`。
- [x] 2.2 只修改 `templates/service-app/app/main.py` 与 `templates/service-app/app/runtime.py`，让 app factory/runtime composition 接受并传递同一 keyword-only `env_file`。
- [x] 2.3 作为该串行共享文件的前置 owner，只修改 `templates/service-app/app/workers/runtime_worker.py` 中 profile/profiles-dir/once/env-file 接线 hunk，让 parse、run-once/run-forever、worker core 与 runtime composition 传递同一 `env_file`；不得混入 private execution-context 恢复逻辑。
- [x] 2.4 只修改 `templates/service-app/tests/test_app_surface.py` 与 `tests/contracts/test_service_app_runtime_entrypoint_contracts.py`，分别保留两份可单独失败的转绿证据：copied app factory 在 clean/冲突 `.env` 下配置与健康结果一致且导入来自复制目录；copied runtime composition settings boundary 在创建 storage/provider 等副作用前接收复制目录的同一显式 env file 并得到一致配置。测试均使用专属空 env file，不读取 ambient `.env`。

## 3. 验证与收口

- [x] 3.1 运行上述聚焦 pytest、相关 Ruff、Pyright、compile 与 workspace 外复制合同并记录红灯转绿。
- [x] 3.2 契约三票通过后先验证仓库外快照，再由主 Agent 独占执行 change matrix `joint-crop-v1`：51个 tracked 路径只恢复已放弃膨胀 hunk、39个 untracked 路径逐项删除、21个保留路径逐 hunk 裁剪；遇到无关用户 hunk立即保留并停止该整项恢复。
- [x] 3.3 联合实现身份`82a78b59…`的fresh Reviewer 1先行、Reviewer 2/3并行，三者对harden、separate和联合范围Stage 1/2均PASS、0 findings；随后`make quality`、`make test`（2475 passed、288 skipped）、`make eval`（11/11）、`make smoke-local`与`make smoke-service`均退出0。`DEV-PLAN.md`保持零diff，change停在ready-to-archive。
