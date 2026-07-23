# Eval and Observability loop

[English](eval-observability-loop.md) | [简体中文](eval-observability-loop.zh-CN.md)

Audience: application developers and scaffold maintainers responsible for trace-to-eval, experiments, provider fan-out, and human acceptance.

Navigation: [root README](../README.md) · [Build an Agent](building-an-agent.md) · [extension guide](extension-guide.md) · [adapter contracts](adapter-contracts.md) · [context/trust boundaries](context-and-trust-boundary.md) · [security policy](security-policy.md) · [release boundaries](release-process.md)

This document describes the approved-only base path and the experiment loop: case admission, tags, splits, harness manifests, comparison, human acceptance, provider degradation, and their actual runtime boundaries. The implementation is provider-neutral; neither provider adapters nor evaluators can bypass local evidence, policy, or human approval.

## Base path remains unchanged

```text
failed / low-score trace
  -> draft eval case
  -> human review
  -> approved dataset
  -> eval run
  -> score sink
  -> local/JSONL and optional provider evidence
```

Automated detectors write drafts only. An approved eval case requires human review. `make eval` runs approved cases only; an empty approved dataset produces stable `no-approved-cases`, not a fabricated score. Experiments extend this path without allowing drafts to bypass review.

## Experiment loop

```text
approved eval cases
  -> behavior tags and safety gates
  -> optimization / holdout / regression split
  -> baseline harness evaluation
  -> candidate harness evaluation (optional)
  -> per-tag / holdout / failure comparison
  -> human reviewer + policy
  -> immutable accepted/rejected decision + audit
```

The optimization subset measures improvement in target behavior. Holdout and regression are independent overfitting gates; a higher aggregate score is not sufficient for acceptance. Omitting the candidate creates an immutable baseline snapshot only; a candidate cannot be added later to the same experiment.

## Case admission and curation

### Three sources

- Handwritten cases: use only for explicit, reproducible behavior boundaries. A reviewer confirms input, expectation, and tags individually; implementation details cannot become the only correct answer.
- Production traces: redact secrets/private data and apply quality screening before entering the draft queue. Failure, low score, or a human flag can trigger a draft only, never automatic approval.
- External datasets: record source license, transformation rules, and logical evidence refs. Imported cases still pass this project's human review; upstream labels do not make them trusted automatically.

Before entering a split, every source must be visible to the current tenant/Agent/dataset, have status `approved`, contain nonempty closed-enum `metadata.behavior_tags`, contain no secret or `[REDACTED]` marker in payload/metadata, and use no host absolute path as an evidence ref. Any failure is fail-closed.

### Behavior tags

The initial closed set is:

- `tool_selection`
- `retrieval_quality`
- `followup_quality`
- `policy_approval`
- `context_trust_boundary`

Tags live in `metadata.behavior_tags`, not filenames or free-text comments. A case may have multiple tags. Comparison trusts the split's persisted `case_tags`, never temporary tags returned by an evaluator.

### Cleanup criteria

Periodically inspect by tenant, Agent, dataset, and tag:

- Saturated: a case that remains perfect across multiple harness versions and no longer distinguishes candidates should become low-frequency regression or be replaced by a sharper boundary case.
- Duplicate: retain one authoritative version when semantics, expectation, failure mode, and evidence are equivalent; duplicates must not overweight a tag.
- Stale: if production behavior, tool contracts, or policy invalidate an expectation, return the case to draft for review. Never edit the approved payload directly.
- Contaminated: if secrets, absolute paths, provider raw responses, or unclear source licensing appear, remove the case from candidates and follow incident response. Deleting display fields is not enough.

## Split and regression policy

`deterministic_multilabel_v1` is the only current strategy. The same request and eligible membership produce the same `split_id`; optimization and holdout must both be nonempty. Explicit `case_ids`, `critical_case_ids`, and cases matching `metadata_flag` enter regression rather than optimization/holdout sampling.

Key `RegressionPolicy` semantics:

- `max_holdout_regression` is the allowed absolute aggregate-score decline. Failure occurs when `candidate - baseline` is below its negative value.
- `critical_case_ids` and regression cases matching `critical_tags` must pass.
- `case_ids` name target failure modes. Fixing one may support an improvement claim, but cannot override a holdout or critical-regression failure.

A split persists case IDs, authoritative tags, distribution, rejection counts, and safe refs—not full case payloads.

## Harness-version manifest

The manifest covers exactly six behavior-changing inputs:

- `prompt_instruction`
- `tool_descriptions`
- `agent_config`
- `retrieval_config`
- `policy_defaults`
- `model_adapter_settings`

The builder normalizes mappings/lists and calculates per-category checksums plus an overall `version_id`. Persisted manifests contain checksums, redacted diff summaries, and logical evidence refs only—never source text, SDK objects, secrets, or absolute paths. An accepted record binds a version to experiment evidence as a production candidate; it does not edit prompts, tool descriptions, or configuration automatically.

The template's default `RecordedApprovedCaseEvaluator` reads local evidence from approved cases only. It prefers `metadata.experiment_scores[version_id][metric]`; when `exact_match` is absent, it compares payload `output` and `expected`. It cannot infer or execute production configuration from checksums. A real harness executor is injected through the `ExperimentEvaluator` protocol and must still return the same split, profile, metric version, and safe refs.

Evaluator success is not trusted by default. Every case/local evidence ref passes unified secret, host-absolute-path, per-item length, list-count, and aggregate-size gates. The service boundary revalidates DTOs even after adapter mutation. If individually valid lists produce a top-level baseline/candidate/comparison or per-case failure diff exceeding the public 100-item or 16-KiB limits, DTO construction and terminal writes expose only a `db://eval-experiments/<id>` truth ref. Full refs remain in local score summaries; create/show/compare/replay, CLI, and provider payloads share the same bounded result. Invalid input records a bounded `eval.experiment.evidence_invalid` summary without writing raw refs to public responses or provider payloads.

## Comparison and provider degradation

Baseline and candidate use the same split, evaluator profile, and metric versions. Comparison returns per-tag baseline/candidate/delta, holdout delta, regressions, new/fixed failures, nonempty closed reason codes, and a recommendation.

`accept` is an algorithmic recommendation only. Entry points cannot override `reject` or `needs_review`. When failure details exceed the inline limit, responses contain a truncated summary and `failure_details_ref`; complete data stays in local experiment score summaries.

Local database evidence commits before optional provider fan-out. Provider-write failure changes status to `*_with_degradation` with a redacted summary. It cannot delete experiments/comparisons, leak raw responses/credentials, or disguise local-evidence failure as provider degradation.

### Execution claims, replay, and `needs_review`

Split, experiment, and the first private execution claim commit in one transaction, preventing concurrent identical keys from creating orphan splits. The coordinator fences result writes by claim ID and renews the lease during evaluation:

- Replay of the same key/body under a valid claim returns the same `experiment_id` and `running` without invoking evaluator/provider again.
- A false or exceptional heartbeat means the claim is lost. The coordinator stops trusted terminal writes; the repository atomically rejects renewal or result submission after lease expiry even when owner matches.
- Deterministic evaluator failure writes `failed` with a closed code, bounded generic summary, and safe evidence refs. Raw exceptions, provider responses, and large payloads are not persisted.
- Process interruption, claim expiry, or terminal-write failure after evaluator return leaves external side effects unprovable. The experiment becomes `needs_review` and clears the private claim; future replay returns that state without automatic rerun.
- Historical `0009` committed `created` before evaluator execution. A legacy `created` without a claim therefore becomes `needs_review`; it cannot be interpreted as “definitely not executed.”
- `needs_review` is not provider degradation. Maintainers compare experiment ID, split refs, and external evaluator evidence manually. This scope has no force-rerun entry and never edits harness or production configuration automatically.

## Human acceptance

An accepted decision requires, in order:

1. complete comparison with recommendation `accept`;
2. `accepted_harness_version` exactly matching the compared candidate;
3. authenticated identity supplying reviewer/tenant; the body cannot override either;
4. `eval.harness.accept` policy returning allow; deny is 403, `require_approval` is 409, and neither creates a nested approval implicitly;
5. one immutable decision, production binding, and decision audit committed in one UoW/savepoint.

A rejected decision still records reviewer, reason, policy, evidence, and audit, but has no `accepted_harness_version` or production binding. A retry by the same reviewer with the same normalized body returns the existing decision without duplicating audit. A different reviewer/body/version returns 409.

## HTTP and CLI operations

HTTP exposes four EVL-004 paths:

- `POST /api/v1/evals/experiments`, requiring `Idempotency-Key`; create is 201 and safe replay is 200.
- `GET /api/v1/evals/experiments/{experiment_id}`.
- `GET /api/v1/evals/experiments/{experiment_id}/comparison`.
- `POST /api/v1/evals/experiments/{experiment_id}/accept`.

The service profile uses HTTP Bearer, and tenant comes from identity. Cross-tenant and missing resources both return 404. Create/read requires `eval.experiment.create` / `eval.experiment.read` or `*`; acceptance uses the policy seam.

CLI uses the same service/DTO/persistence. Run the local commands from a service-app root only after [service-app first use](../templates/service-app/README.md#first-use-local-profile): fingerprint key, `STORAGE_DSN`, and SQLite migration must be ready. Prepare `experiment.json` according to the DTO below; obtain `EXPERIMENT_ID` from create and `CANDIDATE_VERSION` from the candidate manifest.

```bash
uv run agent-harness eval experiment create \
  --request-file experiment.json \
  --idempotency-key harness-candidate-2026-07-11 \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN"

uv run agent-harness eval experiment show "$EXPERIMENT_ID" \
  --profile local --profiles-dir ./configs/profiles --storage-dsn "$STORAGE_DSN"
uv run agent-harness eval experiment compare "$EXPERIMENT_ID" \
  --profile local --profiles-dir ./configs/profiles --storage-dsn "$STORAGE_DSN"
uv run agent-harness eval experiment accept "$EXPERIMENT_ID" \
  --decision accepted \
  --reason "human-reviewed target tags, holdout, and regression" \
  --accepted-harness-version "$CANDIDATE_VERSION" \
  --reviewer local-reviewer \
  --profile local \
  --profiles-dir ./configs/profiles \
  --storage-dsn "$STORAGE_DSN"
```

`experiment.json` is an `EvalExperimentCreateRequest` containing Agent, dataset, tags, split strategy, baseline manifest, and optional candidate manifest. Do not include tenant, reviewer, secrets, or provider objects. On success, each command prints one stable JSON object. Failure prints a redacted error object with `code`, `message`, and `request_id`, then exits nonzero.

## Controlling documents and change boundaries

The truth sources for this capability are:

- `Product-Spec.md` REQ-016
- `API-Contract.md` EVL-004
- `DEV-PLAN.md` Phase 12.5
- the operation and maintenance boundaries in this document

The capability was delivered in `foundation -> comparison -> API acceptance` dependency order and archived on 2026-07-11 as `2026-07-11-eval-dataset-split-foundation`, `2026-07-11-eval-harness-experiment-comparison`, and `2026-07-11-eval-experiment-api-acceptance`. Main specs are synchronized. Later API/worker separation or release automation cannot retroactively change this loop's public seams or evidence semantics.

## Public seams, validation, and troubleshooting

Public extension points are `ApprovedCaseExecutor`, `ExperimentEvaluator`, `ExperimentEvidencePublisher`, case/dataset/experiment repositories, score sink, `TelemetryFacade`, and `ProviderTelemetryAdapter`. A new evaluator/provider preserves DTO, split, metric-version, local-first persistence, redaction, and degradation semantics; SDK objects and raw responses do not cross the seam.

```bash
make eval        # approved cases only
make test        # eval, experiment, provider, and recovery contracts
make smoke-local # fake model + local JSONL evidence
# Use only for a real API/worker/PostgreSQL/Redis composition:
make smoke-service
```

Evidence includes `tests/contracts/test_eval_gate_trace_loop_contracts.py`, `tests/contracts/test_eval_execution_contracts.py`, `tests/contracts/test_eval_experiment_api_contracts.py`, `tests/contracts/test_eval_experiment_evidence_boundaries_contracts.py`, `tests/contracts/test_observability_local_first_fanout_contracts.py`, and `templates/service-app/eval-cases/`.

Troubleshooting: `no-approved-cases` means no reviewed executable sample exists. For provider degradation, verify local scores/events were committed first. When comparison rejects, inspect holdout, critical regressions, metric versions, and safe refs—not aggregate score alone. `needs_review` means execution side effects cannot be proved and requires manual claim/external-evidence review; there is no force-rerun entry. Current dual CI and release dry-run include eval producer evidence in acceptance and promotion gates, but local contracts and previews do not prove hosted artifact services, remote protections, or real release side effects.
