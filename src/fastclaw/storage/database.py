"""Async database lifecycle with safe SQLite defaults."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fastclaw.storage.models import Base


class Database:
    """Own an async SQLAlchemy engine and session factory."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url, echo=echo)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        if self.engine.url.get_backend_name() == "sqlite":
            self._configure_sqlite()

    def _configure_sqlite(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def set_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def sqlite_integrity_errors(self, session: AsyncSession) -> list[str]:
        if self.engine.url.get_backend_name() != "sqlite":
            return []
        rows = (await session.execute(text("PRAGMA foreign_key_check"))).all()
        return [":".join(str(value) for value in row) for row in rows]

    async def close(self) -> None:
        await self.engine.dispose()
