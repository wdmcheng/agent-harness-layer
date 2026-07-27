# Engineering principles: evolve around invariants

[English](engineering-principles.md) | [简体中文](engineering-principles.zh-CN.md)

Audience: maintainers and AI / Agent contributors who make architectural or cross-module changes to Agent Harness Layer and copied service apps.

Navigation: [root README](../README.md) · [architecture boundaries](architecture/README.md) · [Build an Agent](building-an-agent.md) · [adapter contracts](adapter-contracts.md) · [ADR-0001](adr/0001-p0-service-boundaries.md) · [ADR-0002](adr/0002-vendor-adapter-isolation.md)

This document is an executable maintenance contract for future changes. It distinguishes current strengths, known hotspots, and target dependency rules. A target rule does not claim that every current path already complies with it.

`MUST`, `MUST NOT`, and `SHOULD` are normative. Repository contracts, production code, tests, and accepted ADRs remain the evidence for current behavior.

## 1. Start with the invariant, not the pattern

Before choosing a class, package, or design pattern, write down:

1. the behavior that must remain true, including identity, trust, ordering, transaction, recovery, and failure semantics;
2. the variation axis that is actually changing, such as provider, persistence backend, deployment profile, lifecycle state, or policy;
3. the smallest public seam that can isolate that axis;
4. the first failing contract that will prove the intended change.

Then choose the smallest sufficient implementation. A direct function or existing seam is preferable when there is one behavior and one implementation. Introduce a pattern only when it isolates a demonstrated variation, protects an invariant, or creates a necessary test seam.

This repository does **not** require every module to use a named design pattern. It also does not authorize a package-wide rewrite to make the tree look layered. Prefer a narrow vertical slice, retain compatibility while consumers migrate, and remove the old path only after evidence shows that no consumer needs it.

Warning signs that an abstraction is premature:

- it has only one caller and no credible second implementation or policy boundary;
- it forwards every method and leaks the same concrete types;
- it changes names or directories without reducing coupling;
- it hides order-sensitive side effects or failure semantics;
- its only acceptance claim is “cleaner architecture.”

## 2. Five-layer, two-wing dependency contract

Runtime request flow and source-code dependencies are different views. The runtime may flow from Access to Infra, but core code MUST depend on stable contracts rather than concrete infrastructure.

### Allowed source dependencies

| Area | Owns | May depend on | Must not depend on |
|---|---|---|---|
| 1. Access and interaction | HTTP/CLI input, authentication injection, validation, transport DTO conversion, error envelopes | Runtime facades and public DTO/protocol contracts | Engine implementations, concrete storage, provider SDKs, mutable runtime internals |
| 2. Orchestration and Runtime | run lifecycle, idempotency, checkpoint/resume, approval, delegation, budgets | Engine executor contracts, narrow tool capabilities, repository/provider/facade contracts, `CanonicalEvent` | Template routes, business Agent implementations, vendor SDK objects |
| 3. Engine and cognition | typed business input/output, reasoning and context decisions | Provider-neutral model/retrieval/tool protocols and DTOs | Access code, concrete Infra implementations, ORM sessions, vendor SDKs |
| 4. Tools and capabilities | typed capability schemas, authorization, workspace/policy/HITL enforcement | Narrow policy, audit, workspace, provider, and DTO contracts | Transport routes, unrelated Engine implementations, uncontrolled SDK clients |
| 5. Infrastructure and data | storage, queue, provider, retrieval, secret, and business-system adapters | The public protocols/DTOs it implements and adapter-local vendor libraries | Business Agents, template entry points, or a reverse dependency from core contracts into adapters |
| Left wing: Eval Gate | approved behavior evidence, regression/holdout/acceptance execution | Public Agent/runtime entry points and authorized evidence readers | Private state mutation, direct ORM access, automatic promotion of drafts to approved cases |
| Right wing: Observability | local-first event, usage, audit, trace, and optional provider fan-out | `CanonicalEvent`, authorized readers, `TelemetryFacade`, observability adapter contracts | Raw secrets/provider responses, business decisions, or a provider failure that erases local evidence |

The normal dependency and composition shape is:

```text
Access -> Runtime facade -> Engine executor
                         <-> Tool capability contracts

Runtime / Engine / Tools -> public ports (protocol, repository, provider, facade)
Infra adapters           -> implement those ports
composition root         -> selects implementations, injects them, owns disposal

Eval Gate      -> public execution and evidence-reader seams
Observability  <- CanonicalEvent / TelemetryFacade from every relevant stage
```

`Engine <-> Tools` describes authorized invocation through capability contracts; it does not permit mutual imports between arbitrary implementation modules.

### Rules for every boundary crossing

- Within one process, cross layers only through an approved DTO, protocol, facade, repository/UoW, provider contract, or `CanonicalEvent` seam.
- Across processes, exchange only versioned, serializable DTOs, commands, events, and stable refs. The receiving process reconstructs repositories, providers, clients, and other dependencies in its own composition root.
- DTOs carry validated serializable values and stable refs. ORM sessions, SDK objects, clients, closures, credentials, and mutable process objects do not cross.
- Vendor SDK imports belong only in an approved adapter/integration boundary. Adapters translate requests, responses, errors, redaction, and degradation into provider-neutral contracts.
- The composition root selects concrete implementations and owns their process/profile lifetime, startup, and disposal. Business modules do not construct infrastructure opportunistically.
- Mutable global singletons are forbidden. A process may have one injected instance of a resource, but lifetime cardinality is not permission to expose hidden global state.
- Cross-layer convenience is not a reason to bypass policy, identity, budget, workspace, event, or transaction boundaries.

## 3. Preserve current strengths; isolate current hotspots

### Patterns already worth continuing

| Existing approach | Current value | Continue when |
|---|---|---|
| Provider/Strategy plus Adapter | Model, embedding, retrieval, MCP, queue/runtime, and observability variations can stay provider-neutral | A real variation shares one behavioral contract and adapters can close vendor-specific errors and payloads |
| Repository and Unit of Work | SQLAlchemy queries and commit/rollback ownership stay behind storage seams | A use case needs persistence or one explicit atomic boundary; return DTOs/records rather than sessions |
| Facade | `TelemetryFacade` and public runtime seams coordinate policy, evidence, and provider-neutral behavior | Several ordered collaborators implement one cohesive use case |
| Factory and composition root | Service/CLI runtime construction and factories select profile-specific implementations | Construction needs configuration, lifecycle ownership, or a controlled family of collaborators |
| EventBus and `CanonicalEvent` | Runtime, API, and eval publish stable local-first evidence without binding callers to a telemetry vendor | The fact is durable/auditable and consumers do not control the command result |
| Object capability and bound execution services | An execution receives only the capabilities that the composition path grants | Capabilities are explicit, narrow, typed, and cannot be forged from request data |
| Transactional outbox, idempotent recovery, and Saga-like lifecycle | Approval, queue, run, budget, delegation, and publication steps can recover without blindly replaying side effects | A multi-step durable workflow spans transactions or processes and has explicit ownership, compensation/reconciliation, and terminal rules |

“Saga-like” describes the existing durable lifecycle discipline; it does not claim that every workflow implements a general Saga framework.

### Hotspots to evolve locally

| Hotspot | Current signal | Preferred local direction |
|---|---|---|
| `registry/runtime/tools/adapters` | Discovery, execution, capability construction, and vendor integration have adjacent ownership and a wide navigation surface | Clarify one change axis at a time; keep registry metadata, runtime lifecycle, capability policy, and vendor translation separate behind existing public seams before considering moves |
| `models/events/observability/storage` | Invocation, usage/capacity, event publication, telemetry, and durable state share ordering and transaction semantics | Extract only narrow coordination ports/facades that preserve local-first evidence and atomic invariants; do not create a generic “platform service” |
| Concrete `SQLAlchemyStorage` outside `storage` | Many non-storage collaborators accept the concrete engine/UoW factory | Migrate one use case at a time to the smallest repository/UoW/capability protocol; keep concrete construction at composition roots and retain compatibility until callers move |
| `AgentExecutionContext` string service lookup | Bound services are granted through `Mapping[str, object]` and recovered by string name | Stop adding ungoverned string keys; introduce typed capability bundles or narrow protocols per coherent group, with a compatibility adapter during migration |
| Run continuation and state branching | Resume, approval, queue, recovery, and terminal paths accumulate conditional branches | Define states, events, guards, allowed transitions, side effects, and idempotency keys explicitly; move one branch family behind a transition table only after red contract coverage |
| Real model adapters and assembly | The shared execution-service builder currently supports the fake provider and rejects another selection | Add a real provider through a provider-neutral contract, adapter, configuration, redaction/error tests, and composition-root selection; never pass an SDK client into business Agents |

These are prioritization signals, not permission for a big-bang refactor. A hotspot changes only when a product requirement, defect, operability risk, or measured maintenance cost supplies an acceptance target.

## 4. Design principles as project checks

| Principle | Project rule | Review question |
|---|---|---|
| Single Responsibility Principle (SRP) | A module/service owns one cohesive reason to change, not merely one verb or one class | Would a provider upgrade, lifecycle change, and API change edit this unit for unrelated reasons? |
| Open/Closed Principle (OCP) | Add a provider, backend, policy, or handler behind a stable contract when the semantics are genuinely substitutable | Can the new variant be added without branching through every caller? |
| Liskov Substitution Principle (LSP) | Implementations preserve validation, side-effect order, error closure, idempotency, and observability—not only method signatures | Would replacing fake/local with real/service make a previously safe caller leak, retry, or succeed differently? |
| Interface Segregation Principle (ISP) | Consumers receive the smallest capability they use | Does a caller need an entire storage/runtime/service map to perform one repository or provider operation? |
| Dependency Inversion Principle (DIP) | Core policy and lifecycle depend on provider-neutral contracts; adapters depend inward on those contracts | Does a business/runtime module import an ORM, driver, or vendor SDK that the composition root should supply? |
| High cohesion, low coupling | Keep data and rules that change together together; cross ownership through stable seams | Does a change require coordinated edits across unrelated packages or knowledge of private fields? |
| Information hiding | Hide sessions, clients, credentials, lock details, provider payloads, and state representation | Can a caller observe or mutate an implementation detail that the contract does not promise? |
| Explicit side effects | Persistence, queue publication, tool/model calls, approvals, and event fan-out are visible in the use-case sequence | Can a constructor, property, decorator, or implicit lookup trigger external work unexpectedly? |
| Fail closed | Unknown identity, permission, policy, state, contract, or durable outcome blocks or enters a bounded recoverable/`needs_review` state | Could missing configuration, an unknown transition, or provider ambiguity silently allow work? |
| Testable seam | Time, IDs, providers, storage, queues, and external effects are injectable or controlled at a public boundary | Can a contract test exercise success, denial, failure, retry, and recovery without a hidden global or real vendor account? |

Do not deduplicate merely because two blocks look alike. Duplicate code is cheaper than a shared abstraction that joins different invariants or variation axes.

## 5. Pattern selection catalog

| Pattern | Applicable signal | Not applicable signal | Current or candidate landing in this project |
|---|---|---|---|
| Adapter + Strategy | Two or more providers/backends share stable caller semantics; selection varies by profile/configuration | The variants expose different business meaning, or the wrapper would leak raw SDK types | Continue `ModelProvider`, retrieval/embedding providers, event/queue/runtime adapters; add real model support at `adapters/models` plus composition-root selection |
| Factory / composition root | Object graphs vary by profile, require validated configuration, or own startup/disposal | A pure/local object has trivial construction, or a factory only renames its constructor | Continue service-app/CLI runtime composition, `build_agent_execution_services`, and `ToolRegistryFactory`; consolidate lifecycle there rather than adding factories everywhere |
| Repository + Unit of Work | Persistence queries or one atomic transaction must be isolated from use-case policy | Pure computation, transport conversion, or a caller that needs no persistence | Continue storage repositories/UoW; introduce narrow ports to reduce concrete `SQLAlchemyStorage` dependencies outside storage |
| Facade | One cohesive use case coordinates several collaborators in a required order | A “facade” becomes a grab bag, hides unrelated operations, or erases failure details | Continue `TelemetryFacade` and the provider-neutral `RunOrchestrator` seam; consider narrow invocation/publication facades for proven semantic coupling |
| Observer / EventBus | Multiple optional consumers react to a fact; the publisher should remain provider-neutral | The caller needs an immediate command result, or consumer success is required for the transaction | Continue `EventBus` plus `CanonicalEvent`; local durable evidence precedes optional fan-out, and an in-process bus is never a distributed lock |
| State / transition table | A finite lifecycle has repeated state/event/guard combinations and illegal transitions must fail closed | A short linear function has no branching lifecycle, or states cannot be enumerated meaningfully | Candidate for run/approval/continuation/recovery paths; encode allowed transitions and side-effect/idempotency rules, then migrate branch families incrementally |
| Command | Work must cross a queue/process, be retried/replayed, or carry an auditable stable intent | A local synchronous call has no independent lifecycle, or a command would carry mutable process objects | Continue stable run-queue DTOs; candidate typed commands for continuation/publication actions with explicit idempotency and authorization refs |
| Decorator | The same orthogonal concern wraps several interchangeable calls while preserving their contract | Order-sensitive workflow/policy would be hidden, wrappers alter return/error semantics, or retries could duplicate effects | Candidate for bounded metrics, tracing, redaction, timeout, or retry around provider calls; decorator order must be explicit and contract-tested |
| Circuit Breaker / Bulkhead | A remote provider has recurring failure/latency risk and needs bounded concurrency, isolation, or controlled degradation | Local validation, durable evidence writes, or required policy checks; a breaker cannot turn failure into success | Candidate at real model, MCP, telemetry, and future gateway adapters; isolate pools/budgets per dependency and retain local evidence during optional-provider degradation |
| Singleton | A resource has one **process-level lifetime** selected and disposed by the composition root | Hidden global access, mutable registries/caches, test order dependence, or cross-process ownership claims | Describe DB/client/provider lifetime in composition only; inject the instance explicitly. Do not add `get_instance()`, mutable module globals, or service-locator access |

Patterns may be combined only when each has a separate job. For example, a factory may choose an adapter strategy and return it behind a protocol; that does not justify putting policy, retries, lifecycle transitions, and storage into the same factory.

## 6. Architecture change protocol

Follow this order for a dependency, lifecycle, provider, persistence, or boundary change:

1. **State the contract.** Record the invariant, variation axis, affected five-layer/two-wing areas, failure behavior, migration boundary, and evidence required.
2. **Update the source of truth before code.** Update `Product-Spec.md` for product behavior/scope, `API-Contract.md` for public fields or transport behavior, an ADR for a hard-to-reverse architecture decision, and the applicable OpenSpec change for an incremental behavior contract. Do not create or edit unrelated artifacts merely to satisfy a checklist.
3. **Map the current dependency surface.** When `.codegraph/` exists, use CodeGraph before text search or manual file traversal. Identify callers, concrete-type leakage, ownership, and compatibility consumers.
4. **Create the red contract.** Add a failing contract test or checker case that demonstrates the missing behavior/boundary. A prose TODO, a passing test unrelated to the change, or `openspec validate` alone is not red evidence.
5. **Implement the smallest vertical slice.** Keep the old seam as an explicit compatibility adapter when consumers cannot move atomically. Avoid directory-wide moves and speculative interfaces.
6. **Verify deterministically.** Run the focused contract tests, import-boundary checker, static quality checks, and the smallest real integration/smoke path that proves the changed boundary. Reuse neither local mocks nor diagrams as proof of cross-process behavior.
7. **Review frozen evidence and contracts.** Check the implementation against the updated source documents and negative paths. When OpenSpec is used, strict validation and independent contract review happen before implementation/closeout; neither substitutes for the other.
8. **Synchronize explanations.** Update affected English/Chinese docs, ADR links, architecture sources/previews, and migration guidance in the same semantic change. Remove a compatibility path only after callers and tests prove it is unused.

Recommended hotspot sequence, only when a requirement activates it:

1. freeze the present boundary in contract/checker tests;
2. add typed execution capabilities without removing string-service compatibility;
3. move one storage-dependent use case to a narrow repository/UoW port;
4. encode one continuation branch family as explicit transitions;
5. add one real model adapter through the composition root;
6. reassess package ownership using the reduced dependency graph before moving files.

Each step must be independently reviewable and releasable. Later steps are not acceptance criteria for earlier ones.

## 7. Put deterministic rules in checkers and CI

Documentation explains intent and judgment. A mechanically decidable rule is not enforced until a checker or contract test fails when the rule is violated.

Current `scripts/import_boundary_check.py` evidence covers:

- core package metadata must not depend backward on template/examples;
- the service-app template must resolve the core package through its declared dependency/workspace source;
- configured vendor SDK imports must stay in approved adapter/integration paths;
- template app/Agents and examples must not import SQLAlchemy session types directly.

It does **not** prove runtime sandboxing, dynamic-import safety, provider substitutability, state-machine correctness, or that every non-storage module already depends on narrow storage ports.

| Rule kind | Executable enforcement location |
|---|---|
| Forbidden layer/vendor import | Boundary declaration plus `scripts/import_boundary_check.py`, invoked by `make quality`/CI |
| DTO/schema/serialization invariant | Contract schema and round-trip/negative tests |
| Repository/UoW transaction and concurrency invariant | Contract tests plus real PostgreSQL integration where dialect/process behavior matters |
| Allowed lifecycle transition and replay behavior | Transition-table contract tests, including illegal, duplicate, crash, and unknown-outcome cases |
| Fail-closed identity/policy/configuration | Negative API/CLI/runtime contract tests |
| Provider degradation and isolation | Adapter contract tests plus bounded integration tests; local evidence failure remains primary |
| Process lifecycle and disposal | Composition-root tests and the relevant local/service smoke path |

When adding a new mechanically decidable rule, add or extend its checker in the same change before claiming enforcement. Do not add a silent allowlist exception: update the governing contract/ADR, explain the bounded need, and add a regression case.

## 8. Human and Agent completion checklist

Before proposing the design:

- [ ] The invariant and variation axis are explicit.
- [ ] Current callers and ownership were read from the current checkout, using CodeGraph first when indexed.
- [ ] The selected pattern is smaller than the problem and has an explicit non-use case.
- [ ] Five-layer/two-wing dependencies and vendor boundaries remain valid, or an intentional contract change is documented first.

Before implementation is accepted:

- [ ] A red contract/checker preceded the implementation.
- [ ] Side effects, transaction ownership, idempotency, recovery, and failure closure are visible.
- [ ] Concrete adapters are selected and disposed by a composition root; no mutable global singleton was added.
- [ ] Cross-boundary data is provider-neutral and serializable; no session, SDK object, credential, or closure leaked.
- [ ] Focused tests and the relevant real integration/smoke evidence pass.
- [ ] Any mechanically decidable new rule is enforced by checker/CI.
- [ ] English and Chinese maintenance docs are fact-equivalent and mutually linked.

If these checks cannot be answered, stop at planning. Do not compensate for a missing contract by adding more patterns.

## 9. Document maintenance

- Keep this English file and [the Chinese version](engineering-principles.zh-CN.md) fact-equivalent in the same change.
- Link an accepted architecture decision to its ADR and distinguish current implementation from candidate direction.
- Update this document when the selection rule or dependency contract changes; update executable checkers/tests when enforceable syntax changes.
- Use [architecture boundaries](architecture/README.md) for diagram/deployment facts and [adapter contracts](adapter-contracts.md) for concrete seam and validation details. Do not duplicate volatile inventories here.
