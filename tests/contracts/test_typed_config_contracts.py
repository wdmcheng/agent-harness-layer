"""Typed config loader 的公开契约测试。

这些用例故意穿过 `load_settings` seam，而不是测试私有 helper：调用方只关心
profile/agent/env 合并后的 typed settings，以及错误是否能变成可操作诊断。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.config import SettingsLoadError, load_settings

ROOT = Path(__file__).resolve().parents[2]
SERVICE_APP = ROOT / "templates" / "service-app"
PROFILES = SERVICE_APP / "configs" / "profiles"


def test_local_and_service_profiles_load_typed_settings() -> None:
    # service profile 当前只校验部署边界形状，不启动 PostgreSQL、Redis 或 provider。
    local = load_settings(profile="local", profiles_dir=PROFILES)
    service = load_settings(profile="service", profiles_dir=PROFILES)

    assert local.profile == "local"
    assert local.storage.kind == "sqlite"
    assert local.queue.kind == "in-memory"
    assert local.observability.kind == "local-jsonl"
    assert local.model.requires_api_key is False
    assert local.identity.default.tenant_id == "default"

    assert service.profile == "service"
    assert service.service.api_process.enabled is True
    assert service.service.worker_process.enabled is True
    assert service.storage.kind == "postgresql"
    assert service.queue.kind == "redis"


def test_agent_yaml_and_env_file_override_profile_values(tmp_path: Path) -> None:
    profile_path = tmp_path / "local.yaml"
    profile_path.write_text(
        """
profile: local
storage:
  kind: filesystem
  root: .agent-harness/local
queue:
  kind: in-memory
observability:
  kind: local-jsonl
  path: .agent-harness/traces.jsonl
policy:
  provider: yaml
model:
  provider: fake
  requires_api_key: false
identity:
  default:
    tenant_id: default
    user_id: local-user
    session_id: local-session
    roles: [admin]
    permissions: ["*"]
    auth_method: local
""",
        encoding="utf-8",
    )
    agent_path = tmp_path / "agent.yaml"
    agent_path.write_text(
        """
name: research-agent
budget:
  max_tokens_per_run: 4096
tool_allowlist:
  - search
eval_dataset: eval-cases/drafts/research.yaml
delegation_edges:
  - summarizer
""",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_HARNESS_STORAGE__ROOT=.agent-harness/env\n", encoding="utf-8")

    # env file 应覆盖 profile 默认值；agent YAML 只进入 agent 子配置，不污染 profile。
    settings = load_settings(
        profile_path=profile_path,
        agent_config_path=agent_path,
        env_file=env_path,
    )

    assert settings.storage.root == ".agent-harness/env"
    assert settings.agent.name == "research-agent"
    assert settings.agent.budget.max_tokens_per_run == 4096
    assert settings.agent.tool_allowlist == ["search"]
    assert settings.agent.delegation_edges == ["summarizer"]


def test_config_errors_include_field_path_and_hint(tmp_path: Path) -> None:
    # 错误路径测试锁 operator-facing diagnostics，避免泄漏原始 Pydantic/YAML trace。
    profile_path = tmp_path / "broken.yaml"
    profile_path.write_text(
        """
profile: local
queue:
  kind: in-memory
observability:
  kind: local-jsonl
policy:
  provider: yaml
model:
  provider: fake
  requires_api_key: false
""",
        encoding="utf-8",
    )

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile_path=profile_path)

    assert exc_info.value.errors[0].field_path == "storage"
    hint = exc_info.value.errors[0].hint
    assert hint is not None
    assert "profile YAML" in hint
    assert exc_info.value.to_envelope().error.code == "config.invalid"


def test_unsafe_yaml_tags_are_reported_without_construction(tmp_path: Path) -> None:
    profile_path = tmp_path / "unsafe.yaml"
    profile_path.write_text(
        '!!python/object/apply:os.system ["echo unsafe"]',
        encoding="utf-8",
    )

    # `safe_load` 必须把 Python object tags 当成配置错误，而不是构造对象。
    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile_path=profile_path)

    assert exc_info.value.errors[0].code == "config.invalid_yaml"
    assert str(profile_path) in (exc_info.value.errors[0].field_path or "")
