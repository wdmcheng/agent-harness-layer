# 构建、合规与发布边界

适用读者：准备交付构建产物的 scaffold maintainer，以及需要判断复制模板是否达到生产发布条件的 app developer。

导航：[根 README](../README.md) · [架构边界](architecture/README.md) · [安全策略](security-policy.md) · [Eval/Observability](eval-observability-loop.md) · [Redis ADR](adr/0003-redis-runtime-license-policy.md)

## 结论先行

当前 checkout 能人工执行质量、测试、eval、local/service smoke、wheel/sdist 构建和有限的 license/NOTICE/vendoring 检查。它没有 GitHub Actions、GitLab CI、自动版本计算、tag、CHANGELOG 生成、release dry-run、artifact 上传或 registry publish。后者全部属于 Phase 15；本文不提供一个看似可执行但实际不存在的发布流程。

`make build` 使用官方 `uv build` 能力生成本地 wheel/sdist；它不发布。`uv publish` 虽然是 uv 的真实命令，但本仓库尚未定义 registry、credential、审批与发布门禁，因此不得把它当作本项目当前流程执行。参考 [uv 官方 package guide](https://docs.astral.sh/uv/guides/package/)。

## 当前人工门禁

从干净 checkout 按顺序执行：

```bash
uv sync
uv lock --check
make quality
make test
make eval
make smoke-local
# 需要可用的 Docker daemon/Compose；启动真实 PostgreSQL、Redis、migration、API、worker：
make smoke-service
make build
make license-check
```

| 门禁 | 当前证明 | 不证明 |
|---|---|---|
| `make quality` | format、lint、type、import boundary | runtime integration |
| `make test` | unit/contract/离线 integration | 真实 PostgreSQL/Redis/DBOS 跨进程 |
| `make eval` | approved fake-model cases | 生产模型质量或自动 release acceptance |
| `make smoke-local` | SQLite/in-memory/fake model/local JSONL | service profile |
| `make smoke-service` | wheel-only Compose、真实 auth/secret/PostgreSQL/Redis/API/worker/recovery/SSE | 生产部署、容量和高可用 |
| `make build` | `dist/` 中 wheel/sdist 可构建 | 已签名、已上传或可回滚发布 |
| `make license-check` | 根 Apache-2.0 文件、NOTICE 非空、vendored source 目录声明 | SBOM、全部传递依赖许可证兼容或法律意见 |

失败即停止。不要在 service smoke 失败时用 local 结果替代；不要在 license check 通过后声称完成了依赖许可证法律审计。

## 版本真相

| 资产 | 权威来源 | 当前表达规则 |
|---|---|---|
| Python dependency declaration | root/package/template `pyproject.toml` | 写声明范围或 exact pin |
| Python dependency resolution | `uv.lock` | 用 `uv lock --check` 与 `uv tree --locked` 复核实际解析版本 |
| Docker runtime | `templates/service-app/docker-compose.yml` | 按完整 image reference 描述 pin 粒度；`postgres:18` 只固定 major，`redis:8.0.1` 固定该 tag |
| 外部 CLI | 当前开发机 | 仓库未锁定；2026-07-20 本次 Phase 14 验证观察到 Python 3.14.5、uv 0.11.19、Docker 28.1.1、Compose 2.35.1，不能称为项目 pin |
| 实际 server patch | 某次 service smoke 输出 | 只作为该次运行证据，不反向改写 Compose 声明 |

技术栈表与上述受控来源冲突时，修正文档，不偷偷升级 dependency 或 image。Phase 15 如果要保证跨环境一致，必须另行决定 CI runner 和 toolchain pin。

## 构建产物与证据

`make build` 的当前产物是 `packages/agent-harness` 的 wheel 与 sdist，通常位于 `dist/`。交付候选至少记录 commit/diff identity、上述命令退出状态、test/eval/smoke 摘要、service runtime 版本、artifact 文件名和 checksum。当前仓库没有自动收集或发布这些记录；维护者必须保留本次验证日志。

复制的 service-app 不是独立发布证明。它必须通过可信本地 wheel/sdist/source 或组织私有 index bootstrap，并在自己的仓库建立生产配置、license/SBOM、secret、deployment 和 rollback 门禁。

## License 与 NOTICE

本仓库代码按 [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0) 提供，根 `LICENSE` 是许可证正文，`NOTICE` 记录所需声明和来源归属。当前 `scripts/license_check.py` 检查这两个文件及 vendored source 目录命名；它明确不是 SBOM 或传递依赖扫描器。

Redis server 8+ 的官方许可不是 Apache-2.0；Redis 官方说明其开源发行采用 RSALv2、SSPLv1 或 AGPLv3 三选一，而 redis-py client 仍为 MIT。升级 Redis、改变分发/托管用途或准备生产发布前，必须按 [Redis 官方许可说明](https://redis.io/legal/licenses/) 和 [ADR-0003](adr/0003-redis-runtime-license-policy.md) 重新选择适用条款并复核 NOTICE。本文不提供法律意见。

## Phase 15 未来合同

Phase 15 才能新增并证明：GitHub/GitLab 等价 CI、稳定 `make integration`、coverage/test/eval/smoke artifacts、semantic version/release dry-run、tag/CHANGELOG preview、wheel/sdist 上传、私有 registry credential 与审批、发布失败回滚和 P0 acceptance matrix。当前 `.github/workflows`、`.gitlab-ci.yml`、`CHANGELOG.md` 和 release wrapper 均不应被本文假设存在。

## 排障

- `uv lock --check` 失败：先确认 `pyproject.toml` 是否有未锁变更；不要手改 `uv.lock`。
- build 缺包或夹带 workspace 路径：检查 package include、template bootstrap 和 wheel-only smoke。
- service smoke 无法启动：检查 Docker daemon、Compose、端口、secret file 和本轮命名资源；按脚本输出清理，不删除无关 volume。
- license check 失败：补正确来源/许可证/修改说明，或移除不合规 vendoring；不要把目录改名规避检查。
- 需要发布：停止并创建 Phase 15 change，先定义 registry、凭据、审批、版本和回滚合同；不要直接运行 `uv publish`。
