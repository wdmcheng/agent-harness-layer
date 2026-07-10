## 1. CLI 与安全生成器

- [x] 1.1 新增 `scaffold` Typer group 和 `agent` command 的 help/参数 contract test，固定 `<agent_id>`、`--agents-dir`、成功输出和非零错误语义。
- [x] 1.2 实现点分 Python agent id 校验、受控目标解析、existing target 和父路径 symlink escape 拒绝，并用临时目录测试所有非法输入不改变 filesystem。
- [x] 1.3 实现结构化 file renderers，生成 package、typed schemas、受控 executor、fake config、空工具/delegation、draft eval 与空 approved 目录；静态测试证明无 vendor SDK、ORM、secret 或绝对路径。

## 2. 原子发布与当前契约验证

- [x] 2.1 实现 `agents_dir` 扫描根之外的同 filesystem sibling staging、device 校验、registry/config/schema/executor validation、原子 rename 和异常清理；注入写入/验证失败证明不留下目标、staging 或外部改动。
- [x] 2.2 用 `AgentRegistry.load_from_directory` 和 executor resolver 验证成功生成的 descriptor/entrypoint，测试 public descriptor 不泄漏 callable/module/绝对路径。
- [x] 2.3 处理多段 agent id 的必要空父目录和 package import 语义，测试成功发布后立即可由 registry/CLI list 发现，重复运行不覆盖。
- [x] 2.4 增加并发暂停点测试：staging 已含完整/半完整 `config.yaml`、尚未发布时扫描正式 `agents_dir`，断言 staging descriptor 不可见、不产生 validation error；随后分别证明成功 rename 和异常清理。
- [x] 2.5 实现 `--agents-dir` 优先和 service-app/workspace marker root discovery；在 workspace 外复制模板中省略参数必须落到 `<copied-root>/agents`，无唯一 root 时非零退出且不改 filesystem。
- [x] 2.6 让 contract test 通过最终 `AgentRegistry`/executor resolver/`RunOrchestrator` 真实运行生成 agent并获得 terminal output；再经人工 review/approve seam把 draft 放入 approved dataset，由现有 `EvalRunner`执行并留下 score/trace，命令本身不得自动 approve/eval。

## 3. 文档与回归

- [x] 3.1 更新模板 README/docs 的新增 agent 流程，说明生成文件、draft -> human approve 边界、默认无工具权限、运行/eval 命令和无 `--force` 约束。
- [x] 3.2 运行 scaffold targeted tests、完整 CLI help/contract tests、全量测试、quality、build、license、pre-commit 和 OpenSpec strict validation；包含生成并真实运行 agent 后的 rollback compatibility preflight：未迁移时列出 `agent_id`、非零拒绝且文件不变/agent仍可运行，显式迁移或带审计隔离后才放行，记录全部证据。

## 4. Phase 12 三 change 组合验收

- [x] 4.1 在 executor 必填迁移后的最终 registry 中重新证明 basic/fake 与四个 P0 示例全部可由 CLI list，basic/fake 仍满足模板 `AC-006`，四示例各自 run/eval/trace 确定性通过。
- [x] 4.2 在 `APR-002` continuation 代码合入后重跑全量 OpenAPI drift，并证明 approval-gated checkpoint 直接调用公开 `RUN-005` 返回 `409 run.invalid_transition`、token 未消费且 handler 为零；还必须在 waiting checkpoint/approval 持久化后重建 registry、executor resolver、orchestrator 和 approval service，再通过 `APR-002` approve，证明 handler 恰好一次、真实结果和唯一 terminal；同时覆盖 approve/deny并发仲裁、确定性 failed、needs-review 和 private-state 不公开，运行 latest migration 的 `make smoke-service`。
- [x] 4.3 构建最终核心 wheel，把 service-app 复制到 workspace 外并清除源码路径/PYTHONPATH；验证 `.env` 缺失提示、可信 source bootstrap、health、basic run、无参数 scaffold 正确落到 `<copied-root>/agents`，生成 agent 可 list/run，draft 经人工 approve 后 eval 通过。
- [x] 4.4 复扫完整 P0 CLI help（doctor/agents/run/approvals/eval/policy/scaffold 与模板 serve），重跑 scaffold-generated agent rollback fail-closed 组合测试，运行全量 pytest、quality、local/service smoke、四示例 eval、build、license、pre-commit、三个 change strict validate 和 `validate --all --strict`。
- [x] 4.5 在最终 review 前完成三个 change 的其余 task checkbox/验证证据、`DEV-PLAN.md` Phase 12 状态、Product/API 文档和 ready-to-archive 候选说明；冻结最终受审快照并记录待派的 Phase 12 review 命令，确认没有尚待写入的受审内容后勾选本项。

> **外部门禁（不是 task checkbox）：** 4.1-4.5 全部勾选并冻结快照后，主 Agent 才派 fresh code-reviewer 从实现 Stage 1/2 审查整个 Phase 12。任何 finding 修复或 review 后受审内容 diff 都重置 4.1-4.5 与 Stage 1；Stage 1/2 PASS 后只允许设置 clean-state 和创建按 change 分离的本地提交，不再修改受审代码/契约，且不自动 archive、push或部署。ready-to-archive 是该外部门禁 PASS 后的结论，不通过修改 tasks 文件追认。
