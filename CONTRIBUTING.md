# Contributing to Agent Harness Layer

[简体中文](CONTRIBUTING.zh-CN.md)

This guide is the shared contribution contract for humans and Agents working in this
repository. It covers how to establish the current truth, preserve architectural boundaries,
choose sufficient validation, and hand off evidence. It does not grant permission to commit,
push, publish, release, deploy, archive an OpenSpec change, or use production resources.

## Start from the current truth

Before implementation, inspect `git status --short` and the relevant diff. Existing tracked and
untracked work belongs to its owner: do not rewrite, delete, stage, or "clean up" changes outside
your assigned scope.

Read the applicable sources in this order:

1. `Product-Spec.md` defines product requirements and acceptance criteria.
2. `API-Contract.md` defines public HTTP, CLI, and module contracts. Read the relevant section
   when a change crosses one of those seams, and update it only when that public contract changes.
3. `DEV-PLAN.md` defines the phase DAG, ownership, deliverables, and expected evidence.
4. Relevant synchronized `openspec/specs/<capability>/spec.md` files define long-lived OpenSpec
   behavior, while an active `openspec/changes/<change>/` defines one incremental delta. Use
   `uv run openspec list --json` to establish current state, then read the applicable main specs
   and the active change's proposal, delta specs, design, tasks, and metadata that exist. Do not
   create, sync, or archive a change merely because the repository supports OpenSpec.
5. The [engineering principles](docs/engineering-principles.md), relevant architecture guides,
   and ADRs explain established boundaries and trade-offs. Read only the decisions that affect
   the change.

For architecture work that spans sessions, also read and maintain the
[architecture-evolution living plan](docs/plans/architecture-evolution-plan.md) and its
[change matrix](docs/plans/architecture-evolution-change-matrix.md). Reconfirm their frozen
baseline against current Git and OpenSpec state; conversation history is not an authoritative
handoff.

Then inspect the current implementation and tests through the affected public seam. Do not rely on
an old review, generated graph, issue description, or remembered file layout in place of the files
on disk. If two sources conflict, stop and report the exact conflict and its impact; do not silently
choose a winner.

## Define a reviewable change

- State the goal, acceptance criteria, affected public seams, file ownership, and validation plan
  before editing.
- For a behavior change, update its controlling specification first. Do not edit
  `API-Contract.md` when the public API, CLI, and module contract remain unchanged.
- Add a failing contract or regression test through a public seam before changing implementation.
  Valid seams include HTTP, CLI, module protocols, repository or Unit of Work behavior, canonical
  events, and persisted state. A test of a private helper is not a substitute.
- Keep batches small enough to review and revert independently. Do not mix opportunistic cleanup
  into a feature or fix.
- Preserve compatibility intentionally. If a schema, event, CLI flag, migration, or persisted
  value changes, define migration and rollback behavior rather than assuming a clean state.

## Coordinate people, Agents, and worktrees

Parallelism does not remove ownership:

| Need | Mechanism | Rule |
|---|---|---|
| Independent analysis or a fresh judgment | Sub-Agent | Use for cognitive parallelism. Give it the complete relevant contracts and scope; its context is not file isolation. |
| Independent implementation | Separate worktree | Use only after proving no ordering dependency, shared interface, shared acceptance, or file-ownership overlap. Any overlap requires one serial owner or coordinated batch; a worktree is not an independent review. |
| A shared contract, manifest, index, or root document | One serial owner | Other workers provide findings or patches to the owner. Do not edit the same shared file concurrently. |
| Coupled changes across the same seam | One coordinated batch | Keep the spec, red test, implementation, and evidence together so intermediate states do not become false handoffs. |

Every contributor must re-check the current diff before applying a patch. Agents must not revert
another worker's edits, broaden their file scope, or treat access to a tool as authorization to use
it. The coordinating owner is responsible for merging findings, resolving conflicts, and deciding
when the content is frozen for review.

## Code and maintenance style

Chinese is the primary language for maintainer-facing comments, docstrings, test explanations,
script/configuration notes, and operational prose in this project. Keep identifiers, protocol
fields, API names, schema fields, error codes, commands, and other stable technical terms in
English.

Write explanations for maintenance intent, responsibility, data shape, constraints, risks,
compatibility, and trade-offs. Do not narrate obvious code line by line. Non-obvious APIs, workers,
migrations, concurrency paths, security boundaries, test fixtures, and public seams need enough
context for the next maintainer to change them safely.

Use SRP, OCP, DIP, and related principles as diagnostic tools, not as acceptance metrics. The
number of interfaces, layers, or design patterns is never evidence of quality. Prefer the smallest
explicit design that preserves the required boundary. Hidden mutable global singletons are
forbidden; compose stateful dependencies explicitly at an application or adapter boundary.

Follow existing names and public shapes. Avoid unrelated formatting or renaming, speculative
abstractions, and compatibility wrappers with no current requirement.

## Preserve architecture boundaries

| Area | Required boundary |
|---|---|
| Core package | `packages/agent-harness` must not depend on templates, examples, or a concrete business Agent. |
| Application layer | `templates/service-app/app` is a thin HTTP, CLI, worker, and composition layer. It translates protocol DTOs and wires dependencies; business Agent logic does not live there. |
| Business Agents | `templates/service-app/agents` depends only on public harness seams. It must not import vendor SDKs, storage drivers, or ORM sessions directly. |
| Vendor integrations | Vendor SDK imports stay in approved adapter or integration paths behind a project-owned protocol/provider boundary. Vendor objects do not leak into core or business contracts. |
| Persistence | ORM sessions remain inside storage adapters, repositories, migrations, and Unit of Work implementations. Application and Agent code use repository/UoW seams. |
| Cross-layer data | Cross boundaries with explicit DTOs, protocols, facades, providers, repositories/UoW, and `CanonicalEvent`. Do not pass incidental dictionaries, ORM entities, or vendor response objects as hidden contracts. |
| Side effects | External or privileged effects use the policy/HITL/audit path. Policy decides whether human approval is required; the approved action and its outcome remain attributable through audit and canonical event evidence. |

If an architectural rule can be decided mechanically, prose is not enough. Add or update the
checker, a public contract test, and the relevant CI seam in the same change.

## Know the mechanical sources of truth

Do not duplicate tool configuration in documentation:

- `pyproject.toml` is the source of truth for the uv workspace, supported Python/tool versions,
  Ruff, Pyright strictness, pytest discovery, and root environment conventions. Package-level
  `pyproject.toml` files own their package metadata and build configuration.
- `.pre-commit-config.yaml` is the source of truth for the hooks run by pre-commit.
- `scripts/import_boundary_check.py` enforces static dependency direction, approved vendor import
  locations, workspace dependency mapping, and ORM session boundaries. It does not replace runtime
  sandbox, policy, or adapter contract tests.
- `Makefile` provides stable reviewer and CI entrypoints; the scripts and tool configuration behind
  each target define its exact boundary.
- Contract tests under `tests/contracts/` turn public seams and maintenance rules into executable
  evidence.

When a new rule is mechanically decidable, encode it in the appropriate configuration, checker,
contract test, and CI path. Do not leave a durable architecture requirement enforceable only by a
reviewer's memory.

## Select sufficient validation

The stable Make targets prove different things:

| Command | What it covers | What it does not prove |
|---|---|---|
| `make quality` | Pyright environment validation, Ruff format/lint, strict Pyright analysis, and `scripts/import_boundary_check.py`. | Runtime behavior, tests, external services, packaging, or licensing. |
| `make test` | The repository pytest suite under `tests/` using the release dependency group. | A skipped environment-dependent case or real PostgreSQL/Redis service behavior. |
| `make eval` | Local SQLite migration plus the checked-in example evaluation flow and exported score/trace artifacts. | Real-provider quality, hosted observability, or service storage/queue behavior. |
| `make smoke-local` | The offline local profile, core import, template layout, public CLI, fake run, and local event transports without an external model, database, or queue service. | PostgreSQL, Redis, DBOS, API/worker recovery, or production configuration. |
| `make smoke-service` | A newly built core wheel installed into a copied template outside the workspace, then the Compose-backed service path and evidence. It requires usable Docker Compose, PostgreSQL, and Redis. | Production deployment or permission to use production credentials. |
| `make build` | Core package distributions in `dist/` and `dist/SHA256SUMS`. | Fresh-environment installation, publication, or deployment. |
| `make license-check` | License files, locked dependency policy and observations, vendoring records, and approved service image identities; it writes a machine-readable report. | Legal advice or runtime correctness. |

Use the smallest matrix that covers the changed seam:

| Change type | Minimum sufficient validation |
|---|---|
| Documentation only | Check the changed links, language parity, and documented commands; run the relevant documentation contract test; run `git diff --check -- <changed-docs>`. Do not run the full test suite by default. |
| Bounded Python refactor with no behavior change | `make quality` plus the exact affected tests. Add `make test` when the boundary is shared or isolation cannot be demonstrated. |
| Public HTTP, CLI, module, runtime, repository, event, or persisted behavior | The new red contract/regression test, `make quality`, and `make test`; then add the smoke/eval target for the affected runtime below. |
| Local profile, template shell, CLI, fake model, or event transport | Public-behavior row plus `make smoke-local`. |
| Evaluation dataset, scoring, trace, or review flow | Public-behavior row plus `make eval`; also run `make smoke-service` when service storage or queue behavior changes. |
| PostgreSQL, Redis, DBOS, migration, API/worker coordination, durable recovery, or service-only authentication | Public-behavior row plus `make smoke-service`. A SQLite or mocked result is not a substitute. |
| Package metadata or build output | `make quality`, `make test`, and `make build`, plus the consumer/template smoke affected by the change. Building is not publishing. |
| Dependency, vendored code, license policy, or runtime image | Targeted tests and `make license-check`; also run `make smoke-service` before the license gate when service image identity or evidence changes, and `make build` when package contents change. |

For a targeted documentation contract, a current executable example is:

```bash
uv run --group release pytest -q tests/contracts/test_documentation_bilingual_contracts.py
```

Run `uv run pre-commit run --all-files` when preparing an authorized repository-wide handoff or
commit. It reuses the configured mechanical hooks; it is not a substitute for the behavioral
matrix above.

Report what actually ran. For each command, record the command, exit status, concise result, and
the seam it covered. Mark environment failures as `BLOCKED`, test or contract failures as `FAIL`,
and omitted gates as `NOT RUN` with a reason. A passing command supports only its documented
boundary. For documentation-only work, explicitly say that full tests and service smoke were not
run when they were unnecessary.

## Git and security discipline

- Never commit secrets, credentials, `.env`, `.agent-harness/`, database files, trace payloads,
  local state, `.artifacts/` reports, or generated release previews. Follow the repository's
  tracked policy files and `.gitignore`; do not force-add ignored state.
- Do not use production credentials, production data, or a real provider unless the user explicitly
  authorizes that exact boundary. Tests and examples use isolated local state and fake providers.
- Do not commit, amend, push, tag, publish, release, deploy, or archive an OpenSpec change unless
  that exact action is explicitly authorized. Permission for one action does not imply another.
- Before any authorized commit, inspect the staged diff and include only the agreed files. Use an
  atomic Conventional Commit message such as `feat:`, `fix:`, `docs:`, `refactor:`, or `chore:`.
- Generated build or release evidence is verification output, not permission to publish it.

## Handoff checklist

- The accepted requirement and affected public seams are named.
- Applicable sources of truth were read in order, and any conflict is reported.
- Unrelated user and worker changes remain untouched.
- Behavior changes have a public red contract/regression test and an updated controlling spec.
- Package, Agent, vendor, persistence, cross-layer, and side-effect boundaries remain explicit.
- Mechanical rules were updated when the new rule can be enforced automatically.
- Validation results distinguish `PASS`, `FAIL`, `BLOCKED`, and `NOT RUN`, with uncovered boundaries
  stated plainly.
- No secret, local state, trace, database, or release preview is included.
- Commit, push, tag, publish, release, deploy, and OpenSpec archive status are reported as facts,
  never implied.
