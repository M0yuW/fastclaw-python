"""Session persistence boundary used by the agent loop."""

from __future__ import annotations

from typing import Protocol

from fastclaw.storage import Database, SessionRecord, UnitOfWork


class SessionPersistence(Protocol):
    async def load(self, user_id: str, agent_id: str, session_id: str) -> SessionRecord | None: ...
    async def save(self, session: SessionRecord) -> None: ...


class DatabaseSessionPersistence:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def load(self, user_id: str, agent_id: str, session_id: str) -> SessionRecord | None:
        async with UnitOfWork(self._database) as unit:
            return await unit.require_store().get_session(user_id, agent_id, session_id)

    async def save(self, session: SessionRecord) -> None:
        async with UnitOfWork(self._database) as unit:
            await unit.require_store().save_session(session)
