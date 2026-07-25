# ADR-0005: Self-host Redoc static assets

[English](0005-redoc-offline-assets.md) | [简体中文](0005-redoc-offline-assets.zh-CN.md)

- Status: Accepted
- Date: 2026-07-25
- Related: [Product Spec](../../Product-Spec.md) · [ADR-0004](0004-swagger-ui-offline-assets.md)

## Context

FastAPI's default Redoc page loads its JavaScript, favicon, and Google Fonts from external hosts. A `200` response from `/redoc` therefore does not prove that the page works in an offline browser.

## Decision

1. Ship Redoc `2.5.3`'s standalone bundle, upstream package `LICENSE`, and the embedded-dependency license sidecar referenced by the bundle header with the template, and serve the runtime bundle from the FastAPI process by default.
2. After verifying the upstream tarball, deterministically replace the bundle's single fixed Redocly logo URL with a local data URI. If that seam changes, the update fails for maintainer review. The upstream package `LICENSE` and license sidecar remain unchanged.
3. Disable Google Fonts and use the project favicon in both modes. `online` changes only the Redoc JavaScript location, keeps the CDN URL pinned to `2.5.3`, and still validates the local locked asset set before startup.
4. Validate Redoc and Swagger UI separately, then replace both as one staged transaction. Ordinary failures and catchable process interruptions restore the previous complete set. Portable directory replacement requires two renames, so strict atomic visibility to concurrent filesystem observers is not promised; do not update assets while the service is starting or running.

The machine-verifiable vendoring approval is maintained in the paired [Chinese ADR](0005-redoc-offline-assets.zh-CN.md).

## Consequences and review triggers

- Static-file requests, bundle external-URL scanning, and copy-out smoke jointly prove offline Redoc availability; HTML status alone is insufficient.
- Any version, source, file-set, license, or online-delivery change requires renewed review.
