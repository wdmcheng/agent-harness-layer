"""Typed config profile、覆盖与 secret file 合同测试。"""

from __future__ import annotations

from tests.contracts.test_typed_config_contracts import (
    PROFILES as PROFILES,
)
from tests.contracts.test_typed_config_contracts import (
    Path as Path,
)
from tests.contracts.test_typed_config_contracts import (
    SettingsLoadError as SettingsLoadError,
)
from tests.contracts.test_typed_config_contracts import (
    load_settings as load_settings,
)
from tests.contracts.test_typed_config_contracts import (
    pytest as pytest,
)
from tests.contracts.test_typed_config_contracts import (
    secret_files_module as secret_files_module,
)


def test_local_and_service_profiles_load_typed_settings() -> None:
    """本地与服务 profile 必须映射为预期的类型化基础设施边界，读取本身不启动外部依赖。"""

    # service profile 当前只校验部署边界形状，不启动 PostgreSQL、Redis 或 provider。
    local = load_settings(profile="local", profiles_dir=PROFILES)
    service = load_settings(profile="service", profiles_dir=PROFILES)

    assert local.profile == "local"
    assert local.storage.kind == "sqlite"
    assert local.queue.kind == "in-memory"
    assert local.observability.kind == "local-jsonl"
    assert local.model.requires_api_key is False
    assert local.identity.default.tenant_id == "default"
    assert local.service.api_docs.enabled is True
    assert local.service.api_docs.asset_mode == "offline"

    assert service.profile == "service"
    assert service.service.api_process.enabled is True
    assert service.service.worker_process.enabled is True
    assert service.storage.kind == "postgresql"
    assert service.queue.kind == "redis"
    assert service.service.api_docs.enabled is False
    assert service.service.api_docs.asset_mode == "offline"


def test_api_docs_asset_mode_is_typed_and_rejects_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API 文档只允许 offline/online，拼写错误不能静默回退。"""

    monkeypatch.setenv("AGENT_HARNESS_SERVICE__API_DOCS__ASSET_MODE", "online")
    online = load_settings(profile="local", profiles_dir=PROFILES)
    assert online.service.api_docs.asset_mode == "online"

    monkeypatch.setenv("AGENT_HARNESS_SERVICE__API_DOCS__ASSET_MODE", "remote")
    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile="local", profiles_dir=PROFILES)

    assert exc_info.value.errors[0].field_path == "service.api_docs.asset_mode"


def test_api_docs_enabled_accepts_only_typed_boolean_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文档总开关接受显式布尔覆盖，歧义字符串不得静默开启管理面。"""

    monkeypatch.setenv("AGENT_HARNESS_SERVICE__API_DOCS__ENABLED", "false")
    disabled = load_settings(profile="local", profiles_dir=PROFILES)
    assert disabled.service.api_docs.enabled is False

    monkeypatch.setenv("AGENT_HARNESS_SERVICE__API_DOCS__ENABLED", "true")
    enabled_for_service = load_settings(profile="service", profiles_dir=PROFILES)
    assert enabled_for_service.service.api_docs.enabled is True

    monkeypatch.setenv("AGENT_HARNESS_SERVICE__API_DOCS__ENABLED", "sometimes")
    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile="local", profiles_dir=PROFILES)

    assert exc_info.value.errors[0].field_path == "service.api_docs.enabled"


def test_agent_yaml_and_env_file_override_profile_values(tmp_path: Path) -> None:
    """环境文件可覆盖 profile，agent YAML 只能进入 agent 子配置，二者不能互相污染。"""

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


def test_empty_env_value_keeps_optional_setting_unconfigured(tmp_path: Path) -> None:
    """可选配置的空环境值应保持未配置语义，而不是被错误解析为有效空字符串。"""

    profile_path = tmp_path / "local.yaml"
    profile_path.write_text(
        """
profile: local
storage:
  kind: sqlite
  dsn: "sqlite+aiosqlite:///:memory:"
queue:
  kind: in-memory
observability:
  kind: local-jsonl
  path: .agent-harness/traces.jsonl
auth:
  provider: local
  required: false
  dev_bearer_token: null
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
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_HARNESS_AUTH__DEV_BEARER_TOKEN=\n", encoding="utf-8")

    settings = load_settings(profile_path=profile_path, env_file=env_path)

    assert settings.auth.dev_bearer_token is None


def test_test_only_dsn_env_does_not_enter_product_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 service 合同的 DSN 注入不能污染所有 AGENT_HARNESS 配置调用方。"""

    monkeypatch.setenv(
        "AGENT_HARNESS_TEST_POSTGRES_DSN",
        "postgresql+asyncpg://test:test@127.0.0.1:55433/test",
    )

    settings = load_settings(profile="local", profiles_dir=PROFILES)

    assert settings.profile == "local"
    assert settings.storage.kind == "sqlite"


def test_secret_file_maps_to_existing_typed_field_and_strips_one_line_ending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_FILE` 只提供 env 值，字段路径与 schema 仍由 typed settings 决定。"""

    trusted_root = tmp_path / "secrets"
    trusted_root.mkdir()
    secret_path = trusted_root / "storage-dsn"
    secret_path.write_text("postgresql+asyncpg://service:token@db/app \r\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_HARNESS_STORAGE__DSN_FILE", str(secret_path))
    monkeypatch.delenv("AGENT_HARNESS_STORAGE__DSN", raising=False)

    settings = load_settings(
        profile="service",
        profiles_dir=PROFILES,
        secret_root=trusted_root,
    )

    assert settings.storage.dsn == "postgresql+asyncpg://service:token@db/app "


def test_budget_fingerprint_key_is_typed_secret_and_excluded_from_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算指纹 key 只存在于类型化 settings 内存边界，不进入通用 payload。"""

    secret_fixture = "  budget-fingerprint-secret  "
    monkeypatch.setenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY", secret_fixture)
    monkeypatch.delenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE", raising=False)

    settings = load_settings(profile="local", profiles_dir=PROFILES)

    assert settings.budget.fingerprint_key.get_secret_value() == secret_fixture
    assert "fingerprint_key" not in settings.budget.model_dump()
    assert "fingerprint_key" not in settings.budget.to_payload()
    assert secret_fixture not in repr(settings)
    assert secret_fixture not in repr(settings.budget)


def test_budget_fingerprint_key_file_preserves_content_except_one_line_ending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime 不得再 strip 已由 CFG-001 精确解析的 secret 内容。"""

    trusted_root = tmp_path / "secrets"
    trusted_root.mkdir()
    secret_path = trusted_root / "budget-fingerprint"
    secret_path.write_text("  file-budget-secret  \n", encoding="utf-8")
    monkeypatch.delenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY", raising=False)
    monkeypatch.setenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE", str(secret_path))

    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        secret_root=trusted_root,
    )

    assert settings.budget.fingerprint_key.get_secret_value() == "  file-budget-secret  "


def test_budget_fingerprint_key_missing_fails_at_typed_settings_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算指纹密钥缺失必须在 settings 解析时失败，不能拖延到运行时才造成不稳定身份。"""

    monkeypatch.delenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY", raising=False)
    monkeypatch.delenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE", raising=False)

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile="local", profiles_dir=PROFILES)

    error = exc_info.value.errors[0]
    assert error.code == "config.invalid"
    assert error.field_path == "budget.fingerprint_key"


def test_budget_fingerprint_direct_and_file_conflict_uses_cfg001_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一密钥的 direct env 与文件输入冲突需返回稳定错误码，并完全脱敏两侧候选值。"""

    trusted_root = tmp_path / "secrets"
    trusted_root.mkdir()
    secret_path = trusted_root / "budget-fingerprint"
    secret_path.write_text("file-budget-secret", encoding="utf-8")
    monkeypatch.setenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY", "direct-budget-secret")
    monkeypatch.setenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE", str(secret_path))

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(
            profile="local",
            profiles_dir=PROFILES,
            secret_root=trusted_root,
        )

    error = exc_info.value.errors[0]
    assert error.code == "config.secret_file_conflict"
    assert error.field_path == "budget.fingerprint_key"
    assert "direct-budget-secret" not in str(exc_info.value)
    assert "file-budget-secret" not in str(exc_info.value)


def test_direct_and_secret_file_conflict_fails_before_reading_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署冲突不能被 override 掩盖，也不能把任一候选值写进诊断。"""

    trusted_root = tmp_path / "secrets"
    trusted_root.mkdir()
    secret_path = trusted_root / "storage-dsn"
    secret_fixture = "file-secret-fixture"
    direct_fixture = "direct-secret-fixture"
    secret_path.write_text(secret_fixture, encoding="utf-8")
    monkeypatch.setenv("AGENT_HARNESS_STORAGE__DSN_FILE", str(secret_path))
    monkeypatch.setenv("AGENT_HARNESS_STORAGE__DSN", direct_fixture)

    def fail_if_opened(_path: object, _flags: int) -> int:
        """若冲突检测错误地尝试读取文件就立即失败，证明优先级判断没有暴露 secret 内容。"""

        raise AssertionError("direct/file 冲突必须在读取 secret 前失败")

    monkeypatch.setattr(secret_files_module.os, "open", fail_if_opened)

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(
            profile="service",
            profiles_dir=PROFILES,
            secret_root=trusted_root,
            overrides={"storage": {"dsn": "override-secret-fixture"}},
        )

    error = exc_info.value.errors[0]
    serialized = str(exc_info.value)
    assert error.code == "config.secret_file_conflict"
    assert error.field_path == "storage.dsn"
    assert error.hint == "只设置 direct env 或对应的 _FILE，移除另一个输入"
    for forbidden in (secret_fixture, direct_fixture, "override-secret-fixture", str(secret_path)):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "case",
    [
        "relative",
        "directory",
        "symlink",
        "outside",
        "unreadable",
        "empty",
        "non_utf8",
        "oversized",
    ],
)
def test_secret_file_rejects_untrusted_path_type_or_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """所有拒绝路径共享稳定、脱敏且可操作的字段级诊断。"""

    trusted_root = tmp_path / "secrets"
    trusted_root.mkdir()
    candidate = trusted_root / case
    outside = tmp_path / "outside-secret"
    outside.write_text("outside-secret-fixture", encoding="utf-8")
    if case == "relative":
        raw_path = "relative-secret"
    elif case == "directory":
        candidate.mkdir()
        raw_path = str(candidate)
    elif case == "symlink":
        candidate.symlink_to(outside)
        raw_path = str(candidate)
    elif case == "outside":
        raw_path = str(outside)
    elif case == "unreadable":
        candidate.write_text("unreadable-secret-fixture", encoding="utf-8")
        candidate.chmod(0)
        raw_path = str(candidate)
    elif case == "empty":
        candidate.write_bytes(b"")
        raw_path = str(candidate)
    elif case == "non_utf8":
        candidate.write_bytes(b"\xff\xfe")
        raw_path = str(candidate)
    else:
        candidate.write_bytes(b"x" * (64 * 1024 + 1))
        raw_path = str(candidate)
    monkeypatch.setenv("AGENT_HARNESS_STORAGE__DSN_FILE", raw_path)
    monkeypatch.delenv("AGENT_HARNESS_STORAGE__DSN", raising=False)

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(
            profile="service",
            profiles_dir=PROFILES,
            secret_root=trusted_root,
        )

    error = exc_info.value.errors[0]
    serialized = str(exc_info.value)
    assert error.code == "config.secret_file_invalid"
    assert error.field_path == "storage.dsn"
    assert error.hint == ("使用受信 root 内绝对、可读、非空且不超过 64 KiB 的普通 UTF-8 文件")
    for forbidden in ("outside-secret-fixture", str(outside), raw_path):
        assert forbidden not in serialized
