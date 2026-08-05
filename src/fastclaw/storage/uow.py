"""Transactional unit of work."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession

from fastclaw.storage.database import Database
from fastclaw.storage.repositories import SQLAlchemyStore


class UnitOfWork:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.session: AsyncSession | None = None
        self.store: SQLAlchemyStore | None = None

    async def __aenter__(self) -> UnitOfWork:
        self.session = self.database.sessions()
        self.store = SQLAlchemyStore(self.session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self.session is not None
        try:
            if exc is None:
                await self.session.commit()
            else:
                await self.session.rollback()
        finally:
            await self.session.close()

    def require_store(self) -> SQLAlchemyStore:
        if self.store is None:
            raise RuntimeError("unit of work has not been entered")
        return self.store
