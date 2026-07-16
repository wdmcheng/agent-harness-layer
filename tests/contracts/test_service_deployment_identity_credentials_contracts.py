"""Service 持久身份与临时凭据生命周期合同测试。"""

from __future__ import annotations

from tests.contracts.test_service_deployment_compose_contracts import (
    ApiKeyCreate as ApiKeyCreate,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    ApiKeyVerifier as ApiKeyVerifier,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    Path as Path,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    hash_token as hash_token,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    pytest as pytest,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    run_migrations as run_migrations,
)


@pytest.mark.asyncio
async def test_api_key_identity_session_id_fits_persistent_session_contract(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'api-key.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("service-tenant")
            api_key = await uow.api_keys.create(
                ApiKeyCreate(
                    tenant_id="service-tenant",
                    user_id="service-user",
                    name="service-key",
                    token_hash=hash_token("service-token"),
                    roles=["operator"],
                    permissions=["runs:execute"],
                )
            )
            await uow.commit()
        identity = await ApiKeyVerifier(storage).verify("service-token")
    finally:
        await storage.dispose()

    assert identity is not None
    assert identity.session_id == api_key.id
    assert len(identity.session_id) <= 36


@pytest.mark.asyncio
async def test_ephemeral_api_key_can_be_deleted_by_hash(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'api-key-cleanup.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    token_hash = hash_token("ephemeral-service-token")
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("service-tenant")
            await uow.api_keys.create(
                ApiKeyCreate(
                    tenant_id="service-tenant",
                    user_id="service-user",
                    name="ephemeral-key",
                    token_hash=token_hash,
                )
            )
            await uow.commit()
        async with storage.uow() as uow:
            assert await uow.api_keys.delete_by_hash(token_hash) is True
            assert await uow.api_keys.delete_by_hash(token_hash) is False
            await uow.commit()
        assert await ApiKeyVerifier(storage).verify("ephemeral-service-token") is None
    finally:
        await storage.dispose()
