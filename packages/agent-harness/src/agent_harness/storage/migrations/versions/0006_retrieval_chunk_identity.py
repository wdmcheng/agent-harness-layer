"""修正 retrieval chunk identity，纳入 document_id。"""

from __future__ import annotations

from alembic import op

revision = "0006_retrieval_chunk_identity"
down_revision = "0005_retrieval_rag_foundation"
branch_labels = None
depends_on = None

OLD_CONSTRAINT = "uq_retrieval_chunks_tenant_collection_chunk"
NEW_CONSTRAINT = "uq_retrieval_chunks_tenant_collection_document_chunk"


def upgrade() -> None:
    """让同一 collection 下不同 document 可复用 chunk_id。"""

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("retrieval_chunks", recreate="always") as batch_op:
            batch_op.drop_constraint(OLD_CONSTRAINT, type_="unique")
            batch_op.create_unique_constraint(
                NEW_CONSTRAINT,
                ["tenant_id", "collection", "document_id", "chunk_id"],
            )
        return

    op.drop_constraint(OLD_CONSTRAINT, "retrieval_chunks", type_="unique")
    op.create_unique_constraint(
        NEW_CONSTRAINT,
        "retrieval_chunks",
        ["tenant_id", "collection", "document_id", "chunk_id"],
    )


def downgrade() -> None:
    """回滚到早期 chunk_id-only collection identity。"""

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("retrieval_chunks", recreate="always") as batch_op:
            batch_op.drop_constraint(NEW_CONSTRAINT, type_="unique")
            batch_op.create_unique_constraint(
                OLD_CONSTRAINT,
                ["tenant_id", "collection", "chunk_id"],
            )
        return

    op.drop_constraint(NEW_CONSTRAINT, "retrieval_chunks", type_="unique")
    op.create_unique_constraint(
        OLD_CONSTRAINT,
        "retrieval_chunks",
        ["tenant_id", "collection", "chunk_id"],
    )
