# ADR-0004: Self-host Swagger UI static assets

[English](0004-swagger-ui-offline-assets.md) | [简体中文](0004-swagger-ui-offline-assets.zh-CN.md)

- Status: Accepted
- Date: 2026-07-25
- Related: [Product Spec](../../Product-Spec.md) · [ADR-0005](0005-redoc-offline-assets.md)

## Context

FastAPI loads Swagger UI JavaScript, CSS, and a favicon from external hosts by default. `/docs` can therefore return `200` while remaining unusable in a browser without Internet access. A copied service-app and its local profile are expected to work offline, so the API documentation must keep the same boundary.

## Decision

1. Ship Swagger UI `5.32.11` assets, the upstream package `LICENSE` / `NOTICE`, and the embedded-dependency license sidecar referenced by the bundle header with the template, and serve the runtime assets from the FastAPI process by default.
2. `service.api_docs.enabled` controls OpenAPI, Swagger UI, Redoc, the OAuth2 redirect, and the local static mount as one surface. Local defaults to enabled and service defaults to disabled. Disabled applications do not read or validate the asset tree.
3. `service.api_docs.asset_mode=online` changes only the delivery location when docs are enabled. CDN URLs remain pinned to `5.32.11`; floating `latest` or major-only URLs are forbidden.
4. `scripts/update_api_docs_assets.py --update` extracts an exact npm tarball, checks npm sha512 integrity, generates per-file SHA-256 records, and replaces the asset set only after the complete staged set passes validation.
5. An upgrade updates this ADR, `compliance/third-party.toml`, NOTICE, and both template READMEs, then runs the asset check, license gate, and copy-out smoke.

The machine-verifiable vendoring approval is maintained in the paired [Chinese ADR](0004-swagger-ui-offline-assets.zh-CN.md).

## Consequences and review triggers

- Copied projects no longer need browser Internet access for Swagger UI, at the cost of a larger template artifact.
- Any version, source, file-set, license, or CDN-boundary change requires renewed compliance and real copy-out validation.
