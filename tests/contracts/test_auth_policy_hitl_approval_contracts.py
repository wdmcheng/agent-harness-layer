"""HITL approval API 与 CLI 合同测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import (
    PROFILES,
    ROOT,
    asgi_request,
    descriptor,
    sqlite_dsn,
    table_count,
    table_json_payloads,
)

from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import InvalidRunTransition, RunOrchestrator
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from app.main import create_app

__all__ = [
    "AgentRegistry",
    "Any",
    "EventBus",
    "IdentityContext",
    "InvalidRunTransition",
    "LocalJsonlEventSink",
    "PROFILES",
    "Path",
    "ROOT",
    "RunOrchestrator",
    "SQLAlchemyStorage",
    "asgi_request",
    "cast",
    "create_app",
    "descriptor",
    "json",
    "pytest",
    "run_migrations",
    "sqlite_dsn",
    "subprocess",
    "sys",
    "table_count",
    "table_json_payloads",
]
