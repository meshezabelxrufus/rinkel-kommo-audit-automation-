"""
Agent repository — data access for agents table.

Supports upsert (insert-or-update) pattern for webhook-driven
agent creation where we learn about agents from call data.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from app.repositories.base import BaseRepository


class AgentRepository(BaseRepository):
    """CRUD operations for agents in PostgreSQL."""

    async def upsert(
        self,
        *,
        external_agent_id: str,
        display_name: str,
        email: str | None = None,
        phone_number: str | None = None,
    ) -> dict:
        """
        Insert or update an agent by external_agent_id.

        On conflict (same external_agent_id), updates display_name
        and email if provided. This ensures agents are created
        automatically from webhook data without duplicates.

        Returns the upserted row.
        """
        result = await self.session.execute(
            text("""
                INSERT INTO agents (external_agent_id, display_name, email, phone_number)
                VALUES (:external_agent_id, :display_name, :email, :phone_number)
                ON CONFLICT (external_agent_id) DO UPDATE SET
                    display_name = COALESCE(EXCLUDED.display_name, agents.display_name),
                    email = COALESCE(EXCLUDED.email, agents.email),
                    phone_number = COALESCE(EXCLUDED.phone_number, agents.phone_number),
                    updated_at = NOW()
                RETURNING id, external_agent_id, display_name, email, is_active, created_at
            """),
            {
                "external_agent_id": external_agent_id,
                "display_name": display_name,
                "email": email,
                "phone_number": phone_number,
            },
        )
        row = result.mappings().first()
        self._logger.info(
            "agent_upserted",
            agent_id=str(row["id"]),
            external_agent_id=external_agent_id,
        )
        return dict(row)

    async def get_by_external_id(self, external_agent_id: str) -> dict | None:
        """Fetch an agent by their Rinkel external ID."""
        result = await self.session.execute(
            text("""
                SELECT id, external_agent_id, display_name, email, is_active, created_at
                FROM agents
                WHERE external_agent_id = :external_agent_id
            """),
            {"external_agent_id": external_agent_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def get_by_id(self, agent_id: UUID | str) -> dict | None:
        """Fetch an agent by primary key."""
        result = await self.session.execute(
            text("""
                SELECT id, external_agent_id, display_name, email, is_active, created_at
                FROM agents WHERE id = :id
            """),
            {"id": str(agent_id)},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def list_active(self, *, limit: int = 100, offset: int = 0) -> list[dict]:
        """List all active agents."""
        result = await self.session.execute(
            text("""
                SELECT id, external_agent_id, display_name, email, is_active, created_at
                FROM agents
                WHERE is_active = TRUE
                ORDER BY display_name
                LIMIT :limit OFFSET :offset
            """),
            {"limit": limit, "offset": offset},
        )
        return [dict(row) for row in result.mappings().all()]
