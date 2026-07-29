"""模型 usage 恢复合同共享的最小 durable run 夹具。"""

from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage


async def usage_run(storage: SQLAlchemyStorage) -> str:
    """创建带固定租户、会话和 trace 的最小 run，供 outbox 与容量断言复用。"""

    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        await uow.sessions.ensure(
            SessionCreate(
                session_id="session-a",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id="agent-a",
            )
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id="session-a",
                agent_id="agent-a",
                trace_id="trace-a",
            )
        )
        await uow.commit()
        return run.id


__all__ = ["usage_run"]
