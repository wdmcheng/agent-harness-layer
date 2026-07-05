# Agent Harness Service App Template

This template is the future backend service application shell for Agent Harness Layer. Phase 1 reserves the directory layout and local smoke entrypoint only; it does not implement runtime orchestration, API routes, workers, tools, eval, or storage.

## For Agent App Developers

Use this template as the starting point for an agent service once later phases add runtime behavior.

Current local shell:

```bash
make smoke-local
```

The `local` profile lives at `configs/profiles/local.yaml` and does not require real model keys or external observability providers in Phase 1.

## For Scaffold Maintainers

Keep these boundaries intact:

- `app/api`, `app/cli`, and `app/workers` are entrypoint packages.
- Business agent logic belongs under agent-specific directories in `agents/*`.
- The template depends on `agent-harness` through the workspace package boundary.
- Do not import vendor SDKs directly from template code before the adapter boundary exists.

## Reserved Layout

```text
templates/service-app/
├── app/
│   ├── api/
│   ├── cli/
│   └── workers/
├── agents/
│   └── examples/
├── configs/
│   └── profiles/
├── eval-cases/
│   ├── drafts/
│   └── approved/
├── tests/
├── docs/
├── .env.example
├── Makefile
├── README.md
└── pyproject.toml
```

