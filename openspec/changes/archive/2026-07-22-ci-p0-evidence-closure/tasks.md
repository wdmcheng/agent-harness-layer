## 1. 先建立 pipeline 与证据红合同

- [x] 1.1 新增 job contract 与 GitHub/GitLab pipeline 合同测试，先保留当前缺少 YAML、`make integration`、artifact、权限及完整 Git history/tag 语义的失败输出；以带历史 tag 的本地 bare remote 创建真实 depth-1 clone，保留 shallow 输入会漏基线的 red 证据
- [x] 1.2 新增 evidence runner 合同，覆盖成功、失败仍写 result/log、原退出码传播、相对路径和输入 identity；测试使用公开 CLI seam 与中文意图说明
- [x] 1.3 新增 P0 matrix validator 合同，先证明当前全部 P0 REQ/AC 未形成 production/CI/test/evidence 完整映射

## 2. 建立稳定仓库入口与 artifacts

- [x] 2.1 在 `Makefile` 增加 lock/install、独立 ruff format/lint、pyright、import boundary、unit/contract、integration 和 CI evidence targets；定义 `quality-aggregate`、`test-aggregate` 入口分别真实执行 `make quality`、`make test`，聚合与细粒度结果互不替代
- [x] 2.2 为 eval、smoke-local、smoke-service、build、license、release dry-run 接入统一 evidence runner，生成 `ci-result/v1` result/log，并校验 `release-preview/v1`、`release-promotion/v1`、`license-report/v1` 原生 artifact
- [x] 2.3 生成 JUnit/coverage、trace、eval、smoke logs、wheel/sdist/checksum、license report、release preview 的 artifact contract，并验证全部路径复用前置 release change 已建立的 `.artifacts/` 忽略规则；不得重复修改 `.gitignore`

## 3. 实现 GitHub Actions

- [x] 3.1 新增 `.github/workflows/ci.yml`，以完整 action SHA、`fetch-depth: 0` 与 uv `0.11.29` pin 实现 push/pull_request/manual、完整 history/tags、独立 jobs、等价 DAG、失败日志归档和只读权限
- [x] 3.2 新增 `.github/workflows/release.yml`，dry-run 依赖全部 required gates；manual protected `promote` 独占 `contents: write` 且无 registry secret并上传 `release-promotion/v1`，后续 `publish` 恢复只读并验证该回执、preview 与 artifacts 的完整 identity
- [x] 3.3 用合同测试证明普通 CI 无 write/publish 权限、GitHub release checkout 明确 `fetch-depth: 0` 且 wrapper 前为非 shallow/tag 可见、两套 CI 分别执行 `make quality`/`make test`、细粒度结果独立、artifact upload 不改变失败状态、promotion/publish 不绕过 service smoke/license gate，且 `no-release`/`failed`/陈旧或漂移 receipt 不能授权 publish

## 4. 实现 GitLab CI

- [x] 4.1 新增 `.gitlab-ci.yml`，以 `GIT_DEPTH: "0"` 实现完整 history/tags，并实现 push/merge_request/web trigger、与 GitHub 相同的 Make target/job/DAG/artifact/失败语义和固定 uv/Python 环境
- [x] 4.2 配置 protected default branch 的 manual promotion/publish jobs、`release-promotion/v1` artifact 交接、分权 credential、protected environments 与短期 `CI_JOB_TOKEN` 边界；未具备远端保护时不得自动执行
- [x] 4.3 用联合 contract manifest 逐项比较两套 pipeline，拒绝缺 job、额外放行、needs 漂移、artifact 缺失、权限扩大、GitHub `fetch-depth` 非 0、GitLab `GIT_DEPTH` 非 0 或 release 前仍 shallow/tag 不可见

## 5. 实际执行双 pipeline 语义

- [x] 5.1 使用固定 `act 0.2.88` 执行 GitHub job，记录真实容器命令、仓库 Make gate 与退出状态；checkout、setup-uv `0.11.29`、`make ci-lock` 已退出 0，随后 upload-artifact 因本地 artifact server 不支持 v4 `mime_type` 而失败，因此只认仓库 gate PASS，不写整个 job 或 artifact service PASS
- [x] 5.2 使用固定 `gitlab-ci-local 4.73.0` 启动 GitLab job 并记录 local runner 限制；在隔离副本中按该 runner 的 tracked-file 约束纳入当前 dirty 内容后，固定 trixie arm64 镜像已完成 bootstrap、uv `0.11.29`、`make ci-lock` 与 artifact 导出并退出 0，hosted job 仍未执行
- [x] 5.3 从带历史 tag 的本地 bare remote 注入 depth-1 checkout 和受控 required-gate 失败，分别证明两套配置能补全 history/tags、未补全时不执行 release dry-run、required gate 失败仍保留诊断；清理隔离 clone/container/volume

## 6. P0 矩阵与状态同步

- [x] 6.1 创建 `docs/p0-acceptance-matrix.md`，覆盖 Product-Spec 全部 P0 REQ/AC 的状态、production path、CI job、测试和实际 evidence path，并通过 validator
- [x] 6.2 按实际证据局部更新 `Product-Spec.md`、`Product-Spec-CHANGELOG.md`、`DEV-PLAN.md`、`README.md` 与 `docs/release-process.md`，明确 local/hosted、实现/审查/归档/发布边界；禁止因 local runner 勾选 AC-053/054 hosted PASS

## 7. 收口组合合同与本地验证

- [x] 7.1 运行 lock、quality、test、integration、eval、local/service smoke、build、license、pre-commit、双 release 路径、双 CI contract/local runner、三个 change strict、all strict 与 diff check，记录各入口的实际结果和环境限制
- [x] 7.2 补齐四阶段 plan/execute、`status: planned`、tag 后 `release-build/v1`、受限 execute credential、GitHub 短期 push 认证及 eval/smoke 稳定原生产物合同，保证缺少门禁时在副作用前失败
- [x] 7.3 加固 P0 matrix 与独立 `p0-validate`：拒绝文件级、空壳或语义无关测试映射，固定实际 producer、clean-runner evidence 交接、模板复制运行、复合 SQLite/PostgreSQL 验收与 `.artifacts` 解包根
- [x] 7.4 同步最终本地状态：AC-050/051/055/056/058 按现有实现和本地证据完成，AC-053/054 保持 `hosted-unverified`，不声明 hosted PASS、已归档或已发布
