# ADR-0004：自托管 Swagger UI 静态资源

[English](0004-swagger-ui-offline-assets.md) | [简体中文](0004-swagger-ui-offline-assets.zh-CN.md)

- 状态：Accepted
- 日期：2026-07-25
- 关联：[Product Spec](../../Product-Spec.md) · [ADR-0005](0005-redoc-offline-assets.zh-CN.md)

## 背景

FastAPI 默认 Swagger UI 从 CDN 加载 JavaScript、CSS 和 favicon。这会让 `/docs` 在服务端返回 `200` 时仍可能因浏览器无法访问外网而不可用。service-app 是可复制模板，默认 local profile 已承诺离线运行，API 文档不应例外。

## 决策

1. 模板携带 Swagger UI `5.32.11` 的 `swagger-ui-bundle.js`、`swagger-ui.css`、包级 `LICENSE` / `NOTICE`，以及 bundle 首行引用的内嵌第三方依赖许可证 sidecar，默认从当前 FastAPI 进程提供。
2. `service.api_docs.enabled` 同时控制 OpenAPI、Swagger UI、Redoc、OAuth2 redirect 和本地静态 mount；local 默认开启，service 默认关闭。关闭时不读取或校验资源树。
3. `service.api_docs.asset_mode=online` 只在文档已开启时把加载位置切到 CDN，URL 仍锁定 `5.32.11`，不使用 `latest` 或浮动主版本。
4. `scripts/update_api_docs_assets.py --update` 从精确 npm 版本 tarball 提取资源，校验 npm sha512 integrity，生成逐文件 SHA-256 manifest，并在全部成功后整体替换。
5. 版本更新后同步本 ADR、`compliance/third-party.toml`、NOTICE 和双语模板 README，再运行资源 check、许可证检查与 copy-out smoke。

## 合规批准

```toml vendoring_approval
path = "templates/service-app/app/static/api-docs/swagger-ui"
source_url = "https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.32.11.tgz"
source_revision = "414a60c9a40408d37821297682b8a190a840a79b"
source_sha256 = "966b7c7ea3bc98af2f5f125dac3a971973df20ed1f9c40707d846200d8b462a6"
license_expression = "Apache-2.0"
modified = false
modification_summary_sha256 = "d833797d20e06b7d694eb26d9ad1278f0527f78e20e6b93d5e76b5cb3f7ed7c1"
```

## 后果与复审触发条件

- 复制项目的 Swagger UI 不再要求浏览器出网，代价是模板产物增加约数 MB。
- 任何 Swagger UI 版本、来源、文件集、许可证或 CDN 边界变化都必须重新运行合规和真复制验证。
