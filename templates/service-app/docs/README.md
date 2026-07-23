# Service-app documentation map

[English](README.md) | [简体中文](README.zh-CN.md)

This directory contains application-specific architecture decisions, runbooks, and adapter notes after the template is copied. Repository-level public contracts remain upstream, so this directory records application decisions instead of duplicating the entire source-repository documentation set.

- Put application-specific design and operations guidance here.
- To delegate initialization or feature work to an AI / Agent, explicitly ask it to read the [AI / Agent project guide](ai-agent-guide.md). The [Chinese version](ai-agent-guide.zh-CN.md) contains equivalent content. Both are ordinary opt-in documents, not automatic directory-level instructions.
- Core `agent_harness` public seams are documented in this repository's [architecture boundaries](../../../docs/architecture/README.md), [framework positioning and capability comparison guide](../../../docs/framework-positioning.md), [extension guide](../../../docs/extension-guide.md), [adapter contracts](../../../docs/adapter-contracts.md), [context/trust boundaries](../../../docs/context-and-trust-boundary.md), [security policy](../../../docs/security-policy.md), [Eval/Observability loop](../../../docs/eval-observability-loop.md), [release boundaries](../../../docs/release-process.md), and [ADRs](../../../docs/adr/0001-p0-service-boundaries.md).
- In a standalone copy, replace broken source-repository relative links with a fixed-version internal document or upstream repository URL, and record the depended-on `agent-harness` version.
- Fingerprint key, SQLite migration, and isolated-state prerequisites for local CLI, `make dev`, and run commands are in template [First use](../README.md#first-use-local-profile). Application runbooks cannot omit those fail-closed conditions.
- Update `API-Contract.md` before changing an API, then use OpenAPI drift tests.
- See [example Agents](examples.md) for four runnable examples, approved eval, and safe degradation.
