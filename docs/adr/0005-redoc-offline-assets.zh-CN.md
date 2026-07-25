# ADR-0005：自托管 Redoc 静态资源

[English](0005-redoc-offline-assets.md) | [简体中文](0005-redoc-offline-assets.zh-CN.md)

- 状态：Accepted
- 日期：2026-07-25
- 关联：[Product Spec](../../Product-Spec.md) · [ADR-0004](0004-swagger-ui-offline-assets.zh-CN.md)

## 背景

FastAPI 默认 Redoc 页同时引用 CDN JavaScript、外部 favicon 和 Google Fonts。单独检查 `/redoc` 返回 `200` 不能证明无外网浏览器可用。

## 决策

1. 模板携带 Redoc `2.5.3` 的 `redoc.standalone.js`、包级 `LICENSE`，以及 bundle 首行引用的内嵌第三方依赖许可证 sidecar，默认从当前 FastAPI 进程提供。
2. 更新脚本在校验上游 tarball 后，把 bundle 中唯一固定的 Redocly logo 外链确定性替换为本地 data URI；seam 数量或内容变化时更新失败，要求维护者重新审查。包级 `LICENSE` 与许可证 sidecar 不修改。
3. 无论 offline 还是 online 都禁用 Google Fonts 外链并使用项目自带 favicon；`online` 仅将 Redoc JavaScript 切到锁定 `2.5.3` 的 CDN URL，启动前仍校验本地锁定资源集。
4. Redoc 与 Swagger UI 经同一更新脚本分别校验后一起做事务式替换；普通故障或可捕获进程中断会恢复原资源集。跨平台目录替换需要两次 rename，期间不承诺对并发文件系统观察者严格原子；服务启动和资源更新不得并发执行。

## 合规批准

```toml vendoring_approval
path = "templates/service-app/app/static/api-docs/redoc"
source_url = "https://registry.npmjs.org/redoc/-/redoc-2.5.3.tgz"
source_revision = "1b2591e87291fbf6fe1ad5dce9326a316a54609f"
source_sha256 = "e09cc6eb1af62e493e92ebff1ff98b5917ff4018f24ef9be91a3d97998987a73"
license_expression = "MIT"
modified = true
modification_summary_sha256 = "61526e9c44c24f8f440e646fb2d556140bec83d0227677e1d58bc036117eb9f9"
```

## 后果与复审触发条件

- Redoc 离线可用性由静态请求、bundle 外链扫描和复制项目 smoke 共同验证，不再以 HTML `200` 替代。
- Redoc 版本、来源、文件集、许可证或在线传输边界改变时重新审查。
