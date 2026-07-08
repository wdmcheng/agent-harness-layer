"""Phase 7 internal event visibility contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.phase7_contract_helpers import asgi_request, descriptor, sqlite_dsn

from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import RunOrchestrator
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from app.main import create_app


@pytest.mark.asyncio
async def test_internal_run_events_require_policy_permission(tmp_path: Path) -> None:
    from agent_harness.auth import StaticTokenVerifier
    from agent_harness.policy import PolicyEngine, YamlPolicyProvider

    db_path = tmp_path / "events-auth.db"
    events_path = tmp_path / "events.jsonl"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_bus = EventBus(sink=LocalJsonlEventSink(events_path))
    owner = IdentityContext.local_default(session_id="events-owner-session")
    low_privilege = IdentityContext(
        tenant_id="default",
        user_id="events-low-privilege",
        session_id="events-low-session",
        roles=[],
        permissions=[],
        auth_method="api-key",
    )
    orchestrator = RunOrchestrator(storage=storage, event_bus=event_bus, identity=owner)
    waiting = await orchestrator.start_run(
        agent_id="examples.basic",
        input={"prompt": "pause before internal event"},
        checkpoint_state={"reason": "manual"},
        identity=owner,
    )
    await event_bus.publish(
        tenant_id=owner.tenant_id,
        run_id=waiting.run_id,
        agent_id="examples.basic",
        user_id=owner.user_id,
        event_type=CanonicalEventType.REASONING_DELTA,
        payload={"delta": "internal thought"},
    )
    app = create_app(
        orchestrator=orchestrator,
        event_sink=LocalJsonlEventSink(events_path),
        registry=AgentRegistry([descriptor()]),
        auth_verifier=StaticTokenVerifier(
            {
                "owner-token": owner,
                "low-token": low_privilege,
            }
        ),
        policy_engine=PolicyEngine(provider=YamlPolicyProvider.default()),
    )

    try:
        default_status, default_body = await asgi_request(
            cast(Any, app),
            method="GET",
            path=f"/api/v1/runs/{waiting.run_id}/events",
            headers=[(b"authorization", b"Bearer low-token")],
        )
        low_status, low_body = await asgi_request(
            cast(Any, app),
            method="GET",
            path=f"/api/v1/runs/{waiting.run_id}/events?include_internal=true",
            headers=[(b"authorization", b"Bearer low-token")],
        )
        owner_status, owner_body = await asgi_request(
            cast(Any, app),
            method="GET",
            path=f"/api/v1/runs/{waiting.run_id}/events?include_internal=true",
            headers=[(b"authorization", b"Bearer owner-token")],
        )
    finally:
        await storage.dispose()

    assert default_status == 200
    default_event_types = {event["event_type"] for event in default_body["events"]}
    assert default_event_types == {"run.started"}
    assert all(event["visibility"] == "public" for event in default_body["events"])
    assert "resume-" not in json.dumps(default_body)
    assert low_status == 403
    assert low_body["error"]["code"] == "policy.denied"
    assert owner_status == 200
    assert "resume-" not in json.dumps(owner_body)
    assert any(event["event_type"] == "checkpoint.created" for event in owner_body["events"])
    assert any(event["event_type"] == "reasoning.delta" for event in owner_body["events"])
