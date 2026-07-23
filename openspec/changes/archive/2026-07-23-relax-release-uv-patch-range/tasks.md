## 1. 上游真相与失败合同

- [x] 1.1 局部更新 Product Spec、DEV-PLAN 和依赖策略文档，把 uv 支持范围统一为 `>=0.11.29,<0.12`，并区分 CI 具体版本与单次 evidence 实际版本。
- [x] 1.2 通过 release 模块公开 seam 新增版本边界和实际 identity 合同，先证明现有 exact `0.11.29` 实现会拒绝 `0.11.31`、把 release manifest 写成常量且不能绑定 publish plan/execute 版本漂移；同时通过替换公开版本解析 seam 证明 `no-release` 必须输出 `uv_version: null`、consumer 拒绝非空值，producer 不调用解析器、不启动任何 uv 子进程也不进入 build。

## 2. 发布工具范围与实际身份

- [x] 2.1 在共享 release contract 中实现 `uv X.Y.Z` 解析与 `>=0.11.29,<0.12` 范围校验，让调用方获得 executable 和实际版本；范围外或畸形输出在副作用前失败。
- [x] 2.2 让 `release` preview 与正式 tag build 记录实际 uv 版本，consumer 接受范围内 patch 并拒绝缺失、畸形或范围外 identity，同时保持精确 Hatchling backend 校验；`no-release` preview 写入 `uv_version: null`，不调用版本解析器、不启动任何 uv 子进程也不进入 build，consumer 拒绝非空值。
- [x] 2.3 让 registry publish plan 把实际 uv 版本纳入动态 approval identity，execute 在读取 credential 或启动 relay 前拒绝 plan/execute 版本漂移。

## 3. 配置与维护文档

- [x] 3.1 将根 `required-version` 下界调整到 `0.11.29`，保持 GitHub setup、GitLab image/digest 具体 `0.11.29` 不变，并更新依赖策略合同说明其不收窄 wrapper 兼容范围。
- [x] 3.2 局部同步双语 README、双语 release process、Product Spec、DEV-PLAN 与 acceptance mapping，说明支持范围、具体 CI 环境、lock package identity 和实际 artifact uv identity 的边界。

## 4. 双版本与回归验证

- [x] 4.1 用仓库固定 uv `0.11.29` 和本机 uv `0.11.31` 分别执行 lock check、release frozen sync、无隔离 build、release dry-run 与范围/identity 定向合同，并核对 build artifact checksum。
- [x] 4.2 运行受影响 release/publish/documentation/CI 合同、quality、全量测试和 OpenSpec strict validation，确认 `uv.lock` package identity 未升级、无真实 provider/registry 副作用。
