"""Async database lifecycle with safe SQLite defaults."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


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
        await anyio.to_thread.run_sync(self._upgrade_schema)

    def _upgrade_schema(self) -> None:
        checkout_root = Path(__file__).resolve().parents[3]
        checkout_config = checkout_root / "alembic.ini"
        if checkout_config.is_file():
            configuration_path = checkout_config
            script_location = checkout_root / "migrations"
        else:
            package_root = Path(__file__).resolve().parents[1]
            configuration_path = package_root / "alembic.ini"
            script_location = package_root / "migrations"
        configuration = Config(configuration_path)
        configuration.set_main_option("script_location", str(script_location))
        configuration.set_main_option("sqlalchemy.url", self.url)
        command.upgrade(configuration, "head")

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
