# Build, compliance, and release boundaries

[English](release-process.md) | [简体中文](release-process.zh-CN.md)

Audience: scaffold maintainers preparing build artifacts, and application developers deciding whether a copied template is ready for production release.

Navigation: [root README](../README.md) · [architecture boundaries](architecture/README.md) · [security policy](security-policy.md) · [Eval/Observability](eval-observability-loop.md) · [Redis ADR](adr/0003-redis-runtime-license-policy.md)

## Bottom line

This checkout can run quality, tests, eval, local/service smoke, wheel/sdist builds, fail-closed license checks, dual-CI contracts, and release dry-runs manually. The repository includes GitHub Actions, GitLab CI, version calculation, CHANGELOG/release-notes previews, artifact handoff, and plan-only-by-default promotion/private-registry seams. Hosted runners, remote environment protection, and real provider/tag/release/publish actions remain unverified and are not executed here.

`make build` uses the official `uv build` capability to create local wheels/sdists; it does not publish. Registry, credential, approval, and release gates are exposed only through side-effect-free `make registry-publish-plan` and protected `make registry-publish-execute`. Maintainers cannot bypass the wrapper with raw `uv publish`. See the [official uv package guide](https://docs.astral.sh/uv/guides/package/).

## Current manual gates

From a clean checkout, run in order:

```bash
uv sync
uv lock --check
make quality
make test
make integration
make eval
make smoke-local
# Requires a Docker daemon/Compose; starts real PostgreSQL, Redis, migration, API, worker:
make smoke-service
make build
make license-check
make ci-contract-check
make release-dry-run
uv run pre-commit run --all-files
```

| Gate | Proves today | Does not prove |
|---|---|---|
| `make quality` | formatting, lint, types, import boundary | runtime integration |
| `make test` | unit/contract/offline integration | real cross-process PostgreSQL/Redis/DBOS |
| `make integration` | isolated integration suite and JUnit/coverage evidence | service profile or hosted runner |
| `make eval` | approved fake-model cases | production-model quality or automatic release acceptance |
| `make smoke-local` | SQLite/in-memory/fake model/local JSONL | service profile |
| `make smoke-service` | wheel-only Compose, real auth/secrets/PostgreSQL/Redis/API/worker/recovery/SSE | production deployment, capacity, or high availability |
| `make build` | buildable wheel/sdist in `dist/` | signing, upload, or rollback readiness |
| `make license-check` | root Apache-2.0 files, `uv.lock` runtime closure, `compliance/third-party.toml`, `licensecheck` observation, version-bound official PyPI observation snapshots for empty/`UNKNOWN` only, NOTICE, vendoring, pinned image identity, and `.artifacts/license/license-report.json` | legal advice, complete SBOM, or hosted-registry license review |
| `make ci-contract-check` | trigger, dependency, artifact, permission, and shared-entry contracts for both pipelines | hosted-runner execution |
| `make release-dry-run` | next SemVer, tag, CHANGELOG/release-notes preview, wheel/sdist/checksum, or explicit `no-release` | actual commit/tag/release/publish |

Stop on failure. Do not replace failed service smoke with local results, and do not describe a passing license check as a complete legal audit of dependency licenses.

Local development accepts uv `>=0.11.19,<0.12`; CI, release wrappers, and reproducible release evidence use `0.11.29` exactly. If the host uses an HTTP proxy, both `NO_PROXY` and `no_proxy` must include `127.0.0.1`/`localhost`; otherwise host HTTP requests in service smoke may be intercepted and return HTML 503 while the container API remains healthy. The 2026-07-22 archive candidate completed real service smoke with `NO_PROXY=127.0.0.1,localhost` and produced a service trace. That proves the local service gate, not hosted runners or production network configuration.

## Version truth

| Asset | Authoritative source | Current expression rule |
|---|---|---|
| Python dependency declaration | root/package/template `pyproject.toml` | bounded ranges for external dependencies; exact current-version pins for the root/template `agent-harness` self-dependency |
| Python dependency resolution | `uv.lock` | exact reviewed `(name, version, source)` identities; verify with `uv lock --check` and `uv tree --locked` |
| Docker runtime | `templates/service-app/docker-compose.yml` and `compliance/third-party.toml` | PostgreSQL `18.4` and Redis `7.2.14` use full OCI index digests; service smoke still records actual server versions |
| uv CLI | root `pyproject.toml`, GitHub setup action, GitLab image | local `required-version` is `>=0.11.19,<0.12`; both CI systems, release wrapper, and `uv publish` use `0.11.29` exactly |
| Other external CLI | current development host or CI runner | Docker/Compose host tools are not pinned by Python lock; evidence records actual versions rather than claiming project pins |
| Actual server patch | one service-smoke output | evidence for that run only; does not rewrite the Compose declaration |

When a technology-stack table conflicts with a controlled source above, fix the document rather than silently upgrading a dependency, toolchain, or image. Widening a declaration must not change lock identities; dependency upgrades require an explicit `uv lock --upgrade` change. A uv CLI upgrade updates the local range, both CI pins, release contract, and lock validation together. Docker/Compose hosted-runner capability remains unverified.

## CI and release-tool choices

The table records versions or immutable pins used by the repository. Stable-CLI tools use versions; GitHub Actions use full commit SHAs; GitLab and service runtimes use OCI digests so floating tags cannot change execution without review.

| Tool | Version or pin | Selection basis and official source | `uv workspace` fit |
|---|---|---|---|
| uv | local `>=0.11.19,<0.12`; CI/release `0.11.29` | [uv 0.11.29 metadata](https://github.com/astral-sh/uv/blob/0.11.29/pyproject.toml), [`uv lock --check`](https://docs.astral.sh/uv/concepts/projects/sync/#checking-the-lockfile), and [build/publish guide](https://docs.astral.sh/uv/guides/package/); the local patch range is tested separately from the exact release baseline | workspace development uses root `tool.uv.sources`; publication compatibility uses workspace-outside default-isolation builds and installation so metadata cannot depend on local workspace paths |
| python-semantic-release | declaration `>=10.6.1,<11`; lock `10.6.1` | [10.6.1 metadata](https://github.com/python-semantic-release/python-semantic-release/blob/v10.6.1/pyproject.toml) and [version command](https://python-semantic-release.readthedocs.io/en/latest/api/commands.html#semantic-release-version); only Conventional Commits parsing and next-SemVer calculation are reused, while repository wrappers own side-effect-free preview | bounded declaration in the root `release` dependency group, exact identity in `uv.lock`; not a core/template runtime dependency; dry-run never invokes commit/tag/release-writing paths |
| `actions/checkout` | `9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0` | [official commit](https://github.com/actions/checkout/commit/9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0) | checkout only; fixed uv and repository lock still resolve dependencies |
| `astral-sh/setup-uv` | `08807647e7069bb48b6ef5acd8ec9567f424441b` | [official commit](https://github.com/astral-sh/setup-uv/commit/08807647e7069bb48b6ef5acd8ec9567f424441b) and [setup-uv documentation](https://github.com/astral-sh/setup-uv), with `version: 0.11.29` | installs exact uv without changing workspace sources or lock |
| `actions/upload-artifact` | `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` | [official commit](https://github.com/actions/upload-artifact/commit/043fb46d1a93c77aae656e7c1c64a875d1fc6a0a) | transfers `.artifacts/` and `dist/` only; no dependency resolution |
| `actions/download-artifact` | `95815c38cf2ff2164869cbab79da8d1f422bc89e` | [official commit](https://github.com/actions/download-artifact/commit/95815c38cf2ff2164869cbab79da8d1f422bc89e) | consumes upstream artifacts; missing/stale identity cannot advance release gates |
| GitLab uv image | `ghcr.io/astral-sh/uv:0.11.29-python3.12-trixie-slim@sha256:36cdfbf910c8b0f651355c013e7ece9678f4ecbf030a9fd9e6779de421189805` | [uv GitLab integration](https://docs.astral.sh/uv/guides/integration/gitlab/) and [GitLab image syntax](https://docs.gitlab.com/ci/yaml/#image); human-readable tag plus immutable OCI index | image uv is the exact release baseline; mutually exclusive release/license groups are synced separately with the other group excluded |
| act | `0.2.88` | [official act v0.2.88 README](https://github.com/nektos/act/blob/v0.2.88/README.md); local workflow read, dependency parsing, and Docker job execution | validates local container semantics only, not hosted artifact service, permissions, or runner |
| gitlab-ci-local | `4.73.0` | [official 4.73.0 README](https://github.com/firecow/gitlab-ci-local/blob/4.73.0/README.md); local Docker executor and artifact/needs paths | validates local GitLab jobs only; protected variables/environments and hosted runners remain unverified |

GitLab artifact transfer and job ordering follow official [`needs`](https://docs.gitlab.com/ci/yaml/#needs), [`artifacts`](https://docs.gitlab.com/ci/yaml/#artifacts), and [dynamic child-pipeline](https://docs.gitlab.com/ci/pipelines/downstream_pipelines/#dynamic-child-pipelines) semantics. The parent pipeline's credential-free plan produces an environment/secret-free receipt job for `no-release`; publishable input alone creates protected promotion/publish jobs. Real promotion also needs official [protected environments/manual jobs](https://docs.gitlab.com/ci/jobs/job_control/#protect-manual-jobs) and protected-variable configuration. This checkout validates YAML contracts and side-effect-free substitutes only; it does not create remote settings.

### Apple Silicon local-runner boundary

The host in the recorded run was macOS `arm64` with a `linux/arm64` Docker daemon. Local runners default to native `linux/arm64`: act passes `--container-architecture linux/arm64`; GitLab CI local resolves the fixed OCI index digest to the arm64 manifest; service smoke checks actual PostgreSQL/Redis architecture. Pull `linux/amd64` only for an explicit cross-architecture test that records QEMU/emulation prerequisites. An emulated result is not an Apple Silicon native PASS.

act's GitHub artifact-service simulation is not the hosted service. A local artifact-protocol failure records the job/command evidence reached and the runner limitation; it does not make the job PASS. Repository Make gates that exited zero in the container remain valid local evidence, while artifact-service behavior is outside local ready-to-archive acceptance. GitLab CI local similarly cannot prove SaaS runners, protected variables, or environment reviewers.

## Build artifacts and evidence

`make build` produces the `packages/agent-harness` wheel, sdist, and `dist/SHA256SUMS`. `make release-dry-run` additionally produces a `release-preview/v1` manifest, CHANGELOG preview, release notes, and isolated rehearsal artifacts; those wheel/sdist files are not registry-upload input. Protected promotion updates version/CHANGELOG, creates the release commit/tag/notes, then rebuilds formal `release-build/v1` artifacts from the tag target. Registry consumes only that manifest plus `release-promotion/v1`.

A release candidate records at least commit/diff identity, command exit statuses, test/eval/smoke summaries, service runtime versions, artifact filenames, and checksums. CI evidence runners write each gate under `.artifacts/ci/<gate>/`.

A copied service-app is not independent release proof. It bootstraps from a trusted local wheel/sdist/source or organizational private index, then establishes production configuration, license/SBOM, secrets, deployment, and rollback gates in its own repository.

## License and NOTICE

Repository code is under [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0). Root `LICENSE` contains the license; `NOTICE` points to the versioned dependency inventory and no-vendoring fact in `compliance/third-party.toml`. `scripts/license_check.py` fails closed across the `uv.lock` runtime closure, `licensecheck` metadata, dependency policy, exact vendored-source/ADR matches, and Compose image identity. Empty or `UNKNOWN` licensecheck values may be filled only from `compliance/pypi-license-observations.toml` observations bound to name, version, PyPI source, raw fields, and exact-version official JSON. Disagreement with an existing observation, stale identity, or imprecise basis fails; reports include the snapshot checksum. This gate is not legal advice or a complete SBOM.

The versioned official Redis `7.2.14` [`COPYING`](https://raw.githubusercontent.com/redis/redis/7.2.14/COPYING) is recorded as BSD-3-Clause; the redis-py client is recorded independently as MIT. Neither is the repository's Apache-2.0 license. Redis 7.4+ enters another license regime. Before a Redis upgrade, distribution/hosting change, or production release, review NOTICE again against the [Redis license page](https://redis.io/legal/licenses/) and [ADR-0003](adr/0003-redis-runtime-license-policy.md). This document is not legal advice.

## Current release boundary

`.github/workflows/ci.yml` and `.gitlab-ci.yml` share `make ci-*` entry points for lock/install, independent quality gates, unit/contract, integration, eval, local/service smoke, build, license, and release dry-run. Each archives independent result/log and failure diagnostics. The clean-runner `acceptance-validate` jobs download or inherit all matrix producer evidence, including install, integration, and build results that do not exist automatically on a new runner.

`docs/acceptance-matrix.md` explicitly selects long-lived REQs and maps each selected REQ and all its ACs (currently 97) to concrete production files, exact pytest nodes, CI jobs, and actual evidence paths. The validator does not infer scope from development phases or priority labels. It rejects orphan ACs, directory/file-level test mappings, missing/out-of-scope paths, placeholder nodes, and listed acceptance with mismatched producer/behavior nodes. Reviewed import scans, fake adapters/eval, default tenants, deny audit, MCP allowlists, and API/worker/tool/model/event correlation remain mapped to nodes that execute the behavior rather than constants or unrelated happy paths. AC-001 `uv sync --frozen` is proved by install evidence; AC-002 `uv build` by build evidence; AC-003/006 outside-workspace wheel/copied-template execution by integration and real integration tests. AC-012/068 require both the SQLite `test-aggregate` node and real PostgreSQL `smoke-service`; a skipped PostgreSQL pytest is not a complete backend loop. AC-065 maps to a positive single-Agent fake run through a public entry and `smoke-local` proves total latency `<5s`.

`make release-dry-run` uses Conventional Commits and pinned `python-semantic-release==10.6.1` to calculate the next SemVer. Releasable commits produce `release-preview/v1`; no releasable commits succeed without creating tag/release. `make release-promote-plan` creates a versioned plan and GitLab child config. GitHub selects credential-free `promote-no-release` or protected execute from plan output. GitLab dynamic child produces only an environment/secret-free receipt job for `no-release`; `planned` alone produces the four-stage manual gate. `make registry-publish-plan` accepts only promoted formal builds. Protected jobs can cause side effects only after `make release-promote-execute` / `make registry-publish-execute` receive explicit authority, matching preview/promotion receipts, scoped credentials, and a fixed HTTPS endpoint. This checkout executes no real promotion or publish.

Registry upload/check endpoints accept pure HTTPS routes without userinfo, query, or fragment. Tokens enter execute jobs only through dedicated protected-environment variables and never through URL, argv, plan, or logs. GitHub multi-path release artifacts use `.artifacts` as archive root and download back into `.artifacts`; this is a static handoff contract, not proof of the hosted artifact service.

The recorded macOS arm64 local boundary is: act `0.2.88` completed checkout, setup-uv `0.11.29`, and `make ci-lock`, so the GitHub repository gate has local PASS evidence. The full job still failed because act's artifact server lacked upload-artifact v4 `mime_type`; that upload is not a local closeout criterion. GitLab used `gitlab-ci-local 4.73.0` in an isolated copy that included current dirty content under its tracked-file synchronization rules; the fixed Debian trixie arm64 image completed bootstrap, uv `0.11.29`, `make ci-lock`, and artifact export with exit zero. GitHub/GitLab hosted execution, remote protected environments, secret/artifact services, and provider/registry side effects remain unverified.

The historical release, license, and dual-CI changes were synchronized and archived on 2026-07-22. The recorded one-time `owner-waived` decision describes only that frozen candidate's historical review boundary; it is not a future reviewer PASS or default rule. AC-053/054 remain `hosted-unverified` until real hosted pipelines and remote-protection evidence close them.

## Troubleshooting

- `uv lock --check` fails: inspect unmatched `pyproject.toml` changes; never edit `uv.lock` manually.
- Build misses a package or contains a workspace path: inspect package includes, template bootstrap, and wheel-only smoke.
- Service smoke cannot start: inspect Docker daemon, Compose, ports, secret files, and run-scoped resources. Follow script cleanup without deleting unrelated volumes.
- License check fails: add the correct source/license/modification basis or remove noncompliant vendoring; do not rename a directory to evade checks.
- A release is needed: first verify protected environment, endpoint identity, credential scope, approval, version, and rollback contracts. Locally, run dry-run or a loopback substitute—not a real `uv publish`.
