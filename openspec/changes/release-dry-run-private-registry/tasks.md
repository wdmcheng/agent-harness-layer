## 1. 先建立 release 红证据

- [x] 1.1 为临时 git repo 新增 releasable、no-release 与真实 depth-1 shallow clone 合同测试，先保留当前缺少 dry-run/shallow fail-closed seam 的失败输出；测试名与中文 docstring 明确完整 history/tag、版本基线、提交类型和无副作用边界
- [x] 1.2 新增构建产物合同，先证明当前模板 exact workspace 依赖和现有 `make build` 无法生成下一版本 preview/checksum 的未满足证据
- [x] 1.3 新增 registry gate 合同，覆盖缺批准、非 protected ref、credential 泄漏、preview/promotion receipt 身份或 checksum 漂移、非 `promoted` 状态、transient retry 与部分上传不确定状态，并保留 red 输出

## 2. 固定工具与版本真相

- [x] 2.1 在根开发依赖中固定 `python-semantic-release==10.6.1` 与 `licensecheck==2026.0.8`，配置 PSR 的 Conventional Commits、version source、tag format 与显式 `allow_zero_version = true`，并更新 `uv.lock`
- [x] 2.2 校验已有 tag 与无 tag first-release bootstrap 两种基线；用回归合同证明 PSR 10.6.1 默认 `allow_zero_version = false` 会把相同无 tag `feat` 历史算为 `1.0.0`，而受控配置得到 `0.1.0`；wrapper 在调用 PSR 前拒绝 `git rev-parse --is-shallow-repository=true` 且不自行联网，使用 `semantic-release --config <temp> --noop version --print`/`--print-tag`，并在无 `origin` 与拒绝连接代理下证明完整 checkout 不访问远端

## 3. 实现 release dry-run 公开 seam

- [x] 3.1 先在 `.gitignore` 增加唯一的根级 `.artifacts/` 规则并用合同证明路径已忽略，再实现 `scripts/release_dry_run.py`，生成 `release-preview/v1` manifest、提交分类、bump 理由、下一版本和 tag；no-release 路径成功退出且不生成发布物
- [x] 3.2 在临时副本中写入下一版本并用 `uv build` 生成 wheel/sdist、CHANGELOG preview、release notes 与 `SHA256SUMS`，异常和中断后清理临时目录
- [x] 3.3 验证 dry-run 前后 HEAD、refs、tag、tracked diff/status 不变，只允许由本 change 建立的 ignored `.artifacts/**` 输出，并用测试证明成功/失败均不修改外部 registry；后续 changes 不得重复修改 `.gitignore`
- [x] 3.4 用合同测试证明 `ReleaseRecord` 只存在于版本化 CI artifact，不创建 migration/table/repository/UoW，也不连接应用 SQLite/PostgreSQL

## 4. 收紧 package 与 registry 边界

- [x] 4.1 将模板的 `agent-harness` 声明改为可发布兼容范围，同时保留根 `tool.uv.sources` 的 checkout 内 workspace 覆盖
- [x] 4.2 对 wheel METADATA、sdist 和 workspace 外安装做合同验证，拒绝 `workspace = true`、`file://`、绝对路径和相对 path source
- [x] 4.3 实现默认只计划的私有 registry wrapper；execute 需要双重显式授权、protected ref、HTTPS upload/check endpoint、受限环境 credential、匹配的 preview 与 `release-promotion/v1` `promoted` 回执；两个 endpoint 摘要都进入审批身份，只通过固定版本 `uv publish` 生成标准 package-index 请求，并经只监听 loopback、禁止 30x 的受限 relay 转发到冻结 endpoint
- [x] 4.4 用 loopback package-index 替身和假 token 实跑 multipart metadata/bytes；覆盖批准后 upload/check endpoint 分别漂移在网络前失败、认证失败、429/5xx 有界同 checksum 查重重试、400/409、30x 不跟随且不重复发送 body，以及不能确认的部分上传 fail closed，确认日志去敏且无外部副作用

## 5. 实现受保护 promotion seam

- [x] 5.1 新增 promotion 红合同，覆盖缺审批/protected ref/identity、credential 泄漏、release commit/tag/release notes 顺序与部分结果不确定；当前仓库与真实远端必须零副作用
- [x] 5.2 实现默认 plan-only 的 `scripts/release_promote.py`，execute 只消费匹配 checksum 的 `release-preview/v1`，更新 version/CHANGELOG 后创建 release commit、annotated tag 和 provider release notes，并生成绑定 preview checksum、release commit/tag target、provider release 与 artifacts 的 `release-promotion/v1` 回执
- [x] 5.3 用一次性临时 git/bare remote 与 provider HTTP 替身实跑 promotion；覆盖 `promoted`、`no-release`、`failed` 回执以及陈旧 preview/identity/checksum 漂移被 publish 拒绝，测试后清理所有临时 ref/服务，证明不触碰当前仓库、真实 provider 或 registry

## 6. 补齐组合合同并验证

- [x] 6.1 运行 release 聚焦测试、releasable/no-release 两条 dry-run、workspace 外安装、临时 promotion 与 loopback registry 替身，核对稳定 artifact 路径、checksum 和零外部副作用
- [x] 6.2 补齐 `release-promotion-plan/v1`、`release-build/v1`、`release-promotion/v1` 与 `registry-publish-plan/v1` 的状态、审批身份、正式构建和分权 credential 合同
- [x] 6.3 加固 registry/provider 不确定结果、正向 hash 确认、no-release 交接、endpoint credential/漂移、redirect、精确工具版本与完整 plan consumer 校验，任一缺口均在副作用前 fail closed
- [x] 6.4 运行 `uv lock --check`、`make build`、受影响 quality/test 与本 change strict，确认本地 release seam 满足归档前要求
