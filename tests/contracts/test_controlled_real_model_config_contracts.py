"""受控真实模型 typed config、目录与 endpoint 安全合同。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from agent_harness.config import SettingsLoadError, load_settings
from agent_harness.config.model_catalog import model_catalog_digest
from agent_harness.config.model_endpoints import resolve_model_deployment

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


def real_model_override(*, secret: str = "phase18-secret-fixture") -> dict[str, object]:
    """构造完整但不触网的真实 deployment，所有敏感值只存在于测试进程。"""

    catalog = {
        "version": "v1",
        "provider_kind": "openai-compatible",
        "model": "fixture-text-1",
        "request_shape_ref": "single-user-text-no-tools",
        "request_shape_version": "v1",
        "input_bound_strategy_ref": "utf8-bytes-plus-envelope",
        "input_bound_strategy_version": "v1",
        "input_envelope_token_bound": 16,
        "cost_enabled": True,
        "input_token_price_usd": "0.000001",
        "output_token_price_usd": "0.000002",
        "price_source_ref": "fixture-price",
        "price_source_version": "v1",
    }
    catalog["digest"] = model_catalog_digest("fixture_text_1", catalog)
    return {
        "model": {
            "default_deployment_id": "real_primary",
            "credentials": {
                "real_primary_key": {
                    "value": secret,
                    "allowed_origins": ["https://models.example.test"],
                }
            },
            "endpoint_policies": {
                "real_primary_endpoint": {
                    "version": "v1",
                    "provider_kind": "openai-compatible",
                    "allowed_origins": ["https://models.example.test"],
                    "completion_classifiers": [],
                }
            },
            "model_catalogs": {"fixture_text_1": catalog},
            "deployments": {
                "fake_default": {
                    "provider_kind": "fake",
                    "allowed_models": ["fake-local", "fake-scaffold"],
                    "default_model": "fake-local",
                    "fallback_models": [],
                    "max_prompt_utf8_bytes": 8192,
                    "max_output_tokens": 8192,
                    "max_per_attempt_token_bound": 16384,
                    "capabilities": ["text_completion"],
                },
                "real_primary": {
                    "provider_kind": "openai-compatible",
                    "allowed_models": ["fixture-text-1"],
                    "model_catalog_refs": {"fixture-text-1": "fixture_text_1"},
                    "model_catalog_versions": {"fixture-text-1": "v1"},
                    "default_model": "fixture-text-1",
                    "fallback_models": [],
                    "base_url": "https://models.example.test/v1",
                    "endpoint_policy_ref": "real_primary_endpoint",
                    "endpoint_policy_version": "v1",
                    "credential_ref": "real_primary_key",
                    "connect_timeout_ms": 1000,
                    "read_timeout_ms": 2000,
                    "total_timeout_ms": 3000,
                    "max_attempts": 1,
                    "retryable_http_statuses": [],
                    "backoff_initial_ms": 0,
                    "backoff_max_ms": 0,
                    "max_retry_wait_ms": 0,
                    "max_in_flight": 2,
                    "queue_timeout_ms": 100,
                    "max_prompt_utf8_bytes": 1024,
                    "max_output_tokens": 128,
                    "max_per_attempt_token_bound": 1168,
                    "max_per_attempt_cost_bound": "0.001296",
                    "capabilities": ["text_completion"],
                },
            },
        }
    }


def test_settings_merge_controlled_deployment_and_ignore_provider_ambient_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """品牌配置具有唯一权威性，provider 原生环境变量不得进入 typed settings。"""

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-win")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example.test/v1")
    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=real_model_override(),
    )
    resolved = resolve_model_deployment(settings.model, "real_primary")

    assert resolved.credential is not None
    assert resolved.canonical_base_url == "https://models.example.test/v1"
    assert resolved.endpoint_origin == "https://models.example.test"
    assert resolved.credential.get_secret_value() == "phase18-secret-fixture"
    assert "ambient-key-must-not-win" not in repr(settings)
    assert "evil.example.test" not in settings.model.model_dump_json()


def test_real_deployment_loads_complete_typed_policy_without_secret_serialization() -> None:
    """完整 deployment 可解析，公开序列化只保留 credential ref 而不含 secret。"""

    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=real_model_override(secret="unique-phase18-secret"),
    )
    resolved = resolve_model_deployment(settings.model, "real_primary")
    payload = settings.to_payload()

    assert resolved.provider_kind == "openai-compatible"
    assert resolved.model_catalogs["fixture-text-1"].input_token_price_usd == Decimal("0.000001")
    assert resolved.max_per_attempt_token_bound == 1168
    assert "unique-phase18-secret" not in repr(payload)
    assert "value" not in payload["model"]["credentials"]["real_primary_key"]


def test_documented_real_model_fragment_loads_after_branded_secret_injection() -> None:
    """提交的非敏感配置片段必须与 typed schema/digest 同步，不能只是不可执行文档。"""

    fragment_path = ROOT / "templates/service-app/configs/examples/real-text-model.fragment.yaml"
    raw = yaml.safe_load(fragment_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    overrides = cast(dict[str, Any], raw)
    overrides["model"]["credentials"]["real_primary_key"]["value"] = "fixture-secret"

    settings = load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)
    resolved = resolve_model_deployment(settings.model, "real_primary")

    assert resolved.canonical_base_url == "https://models.example.invalid/v1"
    assert resolved.model_catalogs["replace-with-provider-model-id"].cost_enabled is False
    assert resolved.fallback_models == ("replace-with-fallback-model-id",)
    assert resolved.model_catalogs["replace-with-fallback-model-id"].cost_enabled is False
    assert resolved.max_per_attempt_token_bound == 2336


def test_endpoint_and_credential_origin_fail_closed_before_client_or_network() -> None:
    """endpoint 与 credential origin 不一致时在任何 client/DNS/HTTP seam 前拒绝。"""

    overrides = real_model_override()
    credentials = overrides["model"]["credentials"]  # type: ignore[index]
    credentials["real_primary_key"]["allowed_origins"] = [  # type: ignore[index]
        "https://other.example.test"
    ]

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)

    assert exc_info.value.errors[0].code == "config.invalid"
    assert "credential" in (exc_info.value.errors[0].field_path or "")


def test_endpoint_policy_catalog_rejects_unknown_or_mismatched_identity_before_runtime_side_effects() -> (  # noqa: E501
    None
):
    """未知 policy/version/provider/origin 组合必须在 composition 前 fail closed。"""

    overrides = real_model_override()
    deployment = overrides["model"]["deployments"]["real_primary"]  # type: ignore[index]
    deployment["endpoint_policy_version"] = "v2"  # type: ignore[index]

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)

    assert exc_info.value.errors[0].code == "config.invalid"
    assert "endpoint_policy" in (exc_info.value.errors[0].field_path or "")


def test_service_profile_rejects_explicit_loopback_http_before_runtime_side_effects() -> None:
    """明文 loopback 例外只属于 local profile，deployment 不能自行扩大正式部署边界。"""

    overrides = real_model_override()
    model = overrides["model"]  # type: ignore[index]
    deployment = model["deployments"]["real_primary"]  # type: ignore[index]
    deployment["base_url"] = "http://127.0.0.1:8000/v1"  # type: ignore[index]
    deployment["allow_local_http"] = True  # type: ignore[index]
    model["endpoint_policies"]["real_primary_endpoint"]["allowed_origins"] = [  # type: ignore[index]
        "http://127.0.0.1:8000"
    ]
    model["credentials"]["real_primary_key"]["allowed_origins"] = [  # type: ignore[index]
        "http://127.0.0.1:8000"
    ]

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile="service", profiles_dir=PROFILES, overrides=overrides)

    error = exc_info.value.errors[0]
    assert error.code == "config.invalid"
    assert error.field_path == "model.deployments.allow_local_http"


@pytest.mark.parametrize(
    "encoded_parent_segment",
    ["%2e%2e", "%2E%2E", ".%2e", "%2e."],
)
def test_endpoint_rejects_encoded_parent_dot_segment_before_runtime_side_effects(
    encoded_parent_segment: str,
) -> None:
    """编码后的父级 segment 也必须在 client、DNS、HTTP 之前 fail closed。"""

    overrides = real_model_override()
    deployment = overrides["model"]["deployments"]["real_primary"]  # type: ignore[index]
    deployment["base_url"] = (  # type: ignore[index]
        f"https://models.example.test/v1/{encoded_parent_segment}/admin"
    )

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)

    error = exc_info.value.errors[0]
    assert error.code == "config.invalid"
    assert error.field_path == "model.deployments.base_url"


def test_model_catalog_rejects_unknown_mismatched_or_self_reported_bounds_before_reservation() -> (
    None
):
    """目录 digest 与静态 ceiling 必须精确，deployment 不能用内部一致值自证价格。"""

    overrides = real_model_override()
    deployment = overrides["model"]["deployments"]["real_primary"]  # type: ignore[index]
    deployment["max_per_attempt_token_bound"] = 1167  # type: ignore[index]

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)

    assert exc_info.value.errors[0].code == "config.invalid"
    assert "max_per_attempt_token_bound" in (exc_info.value.errors[0].field_path or "")


def test_dynamic_env_alias_and_direct_file_conflicts_fail_before_secret_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """动态 ref 在比较 direct/_FILE 前规范化，大小写别名不能静默覆盖。"""

    monkeypatch.setenv(
        "AGENT_HARNESS_MODEL__CREDENTIALS__Real_Primary_Key__VALUE",
        "direct-secret",
    )
    monkeypatch.setenv(
        "AGENT_HARNESS_MODEL__CREDENTIALS__REAL_PRIMARY_KEY__VALUE_FILE",
        "/path/must/not/be/read",
    )

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile="local", profiles_dir=PROFILES)

    assert exc_info.value.errors[0].code == "config.secret_file_conflict"
    assert "real_primary_key" in (exc_info.value.errors[0].field_path or "")


@pytest.mark.parametrize(
    "env_lines",
    [
        [
            "AGENT_HARNESS_MODEL__DEPLOYMENTS__Real_Primary__MAX_ATTEMPTS=1",
            "AGENT_HARNESS_MODEL__DEPLOYMENTS__REAL_PRIMARY__MAX_ATTEMPTS=2",
        ],
        ["AGENT_HARNESS_MODEL____DEPLOYMENTS__REAL_PRIMARY__MAX_ATTEMPTS=1"],
        ["AGENT_HARNESS_MODEL__DEPLOYMENTS__REAL-PRIMARY__MAX_ATTEMPTS=1"],
    ],
)
def test_dotenv_alias_empty_or_illegal_segments_fail_before_merge(
    tmp_path: Path,
    env_lines: list[str],
) -> None:
    """`.env` 动态路径不能因小写化或丢弃空 segment 而静默覆盖。"""

    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(
            profile="local",
            profiles_dir=PROFILES,
            env_file=env_file,
        )

    assert exc_info.value.errors[0].code == "config.invalid"
    assert "env" in (exc_info.value.errors[0].field_path or "")


def test_dotenv_and_process_env_case_alias_collision_fails_before_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源优先级只覆盖同一原始键，不得掩盖跨来源大小写别名。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGENT_HARNESS_MODEL__DEPLOYMENTS__Real_Primary__MAX_ATTEMPTS=1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "AGENT_HARNESS_MODEL__DEPLOYMENTS__REAL_PRIMARY__MAX_ATTEMPTS",
        "2",
    )

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile="local", profiles_dir=PROFILES, env_file=env_file)

    assert exc_info.value.errors[0].code == "config.invalid"
    assert "env" in (exc_info.value.errors[0].field_path or "")
