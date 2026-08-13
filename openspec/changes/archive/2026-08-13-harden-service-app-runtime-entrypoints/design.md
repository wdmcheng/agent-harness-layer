## Context

核心 `load_settings` 已经支持可选 `env_file`。真实缺陷位于 service-app 的入口接线：Make 没有完整固定 worker 的 profile/profiles-dir/once/env-file，app factory 与 worker 的部分路径也没有把同一 env file 传到现有 runtime composition。修复应复用既有配置加载器和 composition root，不创建第二套配置、验证或部署协议。

## Goals / Non-Goals

**Goals**

- 让复制模板中的 worker 命令完全表达调用方选择的 profile 与目录。
- 保持 service 常驻、非 service 单次消费。
- 让 app/runtime/worker 对同一显式 env file 采用同一语义，并保持省略值兼容。
- 用 workspace 外、fake/local 的聚焦合同锁定原始 Bug。

**Non-Goals**

- 不设计 service smoke 证明平台、边界调用计数、角色能力或 HMAC evidence。
- 不设计 broker/fence、旧 worker 回滚、镜像/Registry、Docker event、FD 生命周期或 AST rollback gate。
- 不改变配置优先级、queue 协议、公开 API、provider 或 service 部署拓扑。

## Decisions

### Decision 1: Make 直接拼出模板内部 worker CLI 参数

`PROFILE` 与 `PROFILES_DIR` 始终显式传递。仅当 `PROFILE != service` 时拼接 `--once`；仅当 `ENV_FILE` 非空时拼接 `--env-file`。`--env-file` 是复制模板内部 `python -m app.workers.runtime_worker` CLI 的向后兼容可选参数；产品公开 `agent-harness` CLI 及其 schema 不变。该接线避免新 wrapper 或隐式环境翻译层。

### Decision 2: env_file 是向后兼容的 keyword-only composition 参数

`create_app`、`build_runtime_components` 及 worker 内部调用链使用 `Path | None`。非 `None` 值原样进入既有 `load_settings`；`None` 不替代默认发现。健康摘要与真实 runtime components 必须由同一次 app 创建选择同一值。

### Decision 3: 测试在 workspace 外复制模板并注入空 env file

合同在临时目录复制模板，放置能改变结果的冲突 `.env`，再显式选择测试专属空文件。测试只观察 Make dry-run 与公开 app/worker composition seam，不增加 production evidence producer、守卫或运维入口。

## Affected Surfaces / 文件所有权

本 change 的实现独占与串行共享范围必须与 change matrix 保持逐路径一致：

- 生产独占：`templates/service-app/Makefile`、`templates/service-app/app/main.py`、`templates/service-app/app/runtime.py`。
- 串行共享验收：`templates/service-app/app/workers/runtime_worker.py`。本 change 先写且只写 profile/profiles-dir/once/env-file 接线 hunk；后置 change 不得改该配置 hunk，也不得增加 private context 或 queue 协议，只允许为既有 queued terminal recovery 转发 queue message 当前 `request_id`，并验证既有 `execute_run` 调用链进入 orchestrator classifier。完成后必须重跑本 change 的聚焦合同。
- 测试：`templates/service-app/tests/test_app_surface.py`、`tests/contracts/test_service_app_runtime_entrypoint_contracts.py`。
- change artifact 与 living plan/matrix 由主 Agent 串行维护。唯一例外是三张契约票通过、仓库外快照验证完成后，主 Agent 按 change matrix `joint-crop-v1` 的逐路径 manifest 恢复/删除已放弃膨胀；该事务不改变任何 HEAD 行为，只移除当前 dirty 扩张。除此之外，除非先修约并重审，不得修改其他生产、测试、脚本、Compose、配置或运维文件。

`joint-crop-v1` 的 owner、51个 tracked 恢复路径、39个 untracked 删除路径与21个逐 hunk 保留路径完整写在 change matrix 5.6.1；本 design 逐值采用该版本，不允许 glob、目录级删除或清单外恢复。发现任一无关用户 hunk 时必须保留该 hunk并停止对应整项恢复。

## Risks / Trade-offs

- **Make 路径含空格**：合同用真实临时路径覆盖，命令必须保持单个参数值。
- **默认发现兼容性**：省略或空 `ENV_FILE` 时不生成 CLI 参数；Python seam 继续传 `None`。
- **配置被加载两次**：app 健康摘要与 runtime components 可能分别调用 loader，但必须使用相同显式参数；本 change 不重构 loader 生命周期。

## Migration Plan

无需数据 migration。契约三票后先验证仓库外快照，再执行 `joint-crop-v1`；随后保留能复现 Make/env-file 漂移的聚焦红灯，只在上述独占/共享 hunk 接通既有 seam 并转绿。回滚时可删除新增参数透传，但不得恢复向错误 profile 运行或受 ambient `.env` 污染的缺陷。

## Open Questions

无阻断性问题。
