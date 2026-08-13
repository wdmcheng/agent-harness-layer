## 1. 红灯与不变性基线

- [x] 1.1 建立真实 CLI → 严格业务 DTO 红灯，证明旧实现自动注入 `source` 会触发 extra-field 错误。
- [x] 1.2 建立业务同名 `source` 字段合同，证明修复不得 pop、过滤或覆盖调用方字段。
- [x] 1.3 建立 private context round-trip 红灯，逐值覆盖 exact `input_provenance={schema_version:"run-input-provenance-v1",source:"cli",execution_request_id:<non-empty-string-or-null>}`、缺失 envelope 的非 CLI/legacy、与顶层 `request_id` 一致性，以及旧键、未知版本/来源、额外/缺失字段、错型、空 ID 和冲突值的失败关闭。
- [x] 1.4 在 `tests/contracts/test_cli_input_provenance_recovery_contracts.py` 建立 pickup/reclaim 后 rebuild、幂等 replay、local 与 service queued terminal recovery、local/service approval resume 红灯，并断言 executor 收到 typed provenance；service DBOS 失败场景必须让 authoritative execution request id 为 `None`，同时断言新 terminal 使用当前 delivery request id。
- [x] 1.5 在 approval resume 红灯中同时提供不同当前 resume request id 与 authoritative nullable execution request id，证明旧实现错误覆盖 executor context；同时断言 APR-002/resumed/terminal 仍使用当前 resume request id，并以service DBOS确定失败分支单独证明补写terminal不会回退到原execution ID。
- [x] 1.6 冻结公开 run/HTTP/OpenAPI、queue DTO、provider request 与 delegation input/hash 不变性。
- [x] 1.7 冻结 `agent_harness.runtime` export 与公开 `RunOrchestrator.start_run`、`resume_run` 参数集合，证明普通公开 caller 不能构造、传入 CLI provenance 或注入当前 resume request id。
- [x] 1.8 建立 guardrail/audit 正负合同：CLI typed provenance 必须逐值进入 guardrail policy context 与 audit evidence；API/internal/delegation 等非 CLI input 即使含业务 `source` 也不得被推断或审计为 CLI，业务字段保持原值。

## 2. 最小实现

- [x] 2.1 只修改 `packages/agent-harness/src/agent_harness/cli.py`、`policy/engine.py`、`runtime/executor.py` 与四个模板适配层 `agents/examples/dev_assistant/agent.py`、`rag_assistant/agent.py`、`repo_analyst/agent.py`、`ticket_triage/agent.py`，删除 transport `source` 注入和 `payload.pop("source", None)` 特判，让 guardrail、audit 与 executor 消费内部 typed provenance；保留适配层其他业务归一化，不修改任何公开业务 schema、`runtime/__init__.py` 或公开 `RunOrchestrator.start_run` 参数集合。
- [x] 2.2 只新增 `runtime/_continuation_context.py` 并修改 `storage/run_repositories.py`、`storage/repositories.py`，在内部下划线模块增加封闭 typed provenance、exact `run-input-provenance-v1` envelope、唯一 classifier 与窄 repository 读取 seam；envelope exact fields 为 `schema_version/source/execution_request_id`，后者必须与顶层 `request_id` 一致，且不得从 queue/approval correlation 回填，不从公开 runtime façade 导出。
- [x] 2.3 只修改 `runtime/_run_lifecycle.py` 与 `runtime/_queued_run_orchestration.py`，通过显式私有 submission seam 接通 local create、幂等 replay、pickup/reclaim 后 rebuild、terminal recovery 与 executor propagation；公开 `start_run` 继续走无 CLI provenance 的既有参数集合。
- [x] 2.4 在 harden 的 profile/profiles-dir/once/env-file hunk完成后，保留 `templates/service-app/app/workers/runtime_worker.py` 的既有业务 input，验证 pickup/reclaim → `execute_run` 进入 orchestrator classifier；DBOS 确定性失败只把 `message.request_id` 转发给既有 orchestrator 私有 queued terminal recovery seam，不增加 private context 参数、queue DTO、claim、ack 或 DBOS 协议，并复验两组聚焦合同。
- [x] 2.5 只修改 `approvals/_continuation.py`、`approvals/_queue_resolution.py` 与 `runtime/_run_continuation.py`：`_continuation.py` 从既有 resolution lease 取得当前 request id并走私有resume seam，`_run_continuation.py`让local/service approval resume重建的executor/continuation context使用classified authoritative nullable execution request id，`_queue_resolution.py`只在DBOS确定失败补写terminal时把已持久化的当前resolution request id交给现有私有evidence恢复seam；APR-002/resumed/terminal均使用当前resolution ID，公开`RunOrchestrator.resume_run`参数集合保持不变。
- [x] 2.6 契约三票通过后先验证仓库外快照，再由主 Agent 独占执行 change matrix `joint-crop-v1`：51个 tracked 路径只恢复已放弃膨胀 hunk、39个 untracked 路径逐项删除、21个保留路径逐 hunk 裁剪；`approvals/_continuation.py` 当前 clean，不计入预裁剪 manifest，裁剪后才按 2.5 修改；遇到无关用户 hunk立即保留并停止该整项恢复。

## 3. 验证与收口

- [x] 3.1 运行上述聚焦 pytest、相关 Ruff、Pyright、compile、strict validate 与 `git diff --check`，记录原始红灯转绿。
- [x] 3.2 在两个独占测试文件逐项证明 guardrail/audit 只消费 typed provenance、executor propagation、公开 schema、provider request、delegation 和既有 queue protocol 未变，且所有真实 provider/外部工具/SaaS 调用为零。
- [x] 3.3 联合实现身份`82a78b59…`的fresh Reviewer 1先行、Reviewer 2/3并行，三者对harden、separate和联合范围Stage 1/2均PASS、0 findings；随后`make quality`、`make test`（2475 passed、288 skipped）、`make eval`（11/11）、`make smoke-local`与`make smoke-service`均退出0。`CLI-RUN-001`已同步为完成，`DEV-PLAN.md`保持零diff，change停在ready-to-archive。
