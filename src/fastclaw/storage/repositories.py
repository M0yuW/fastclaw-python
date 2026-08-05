"""Small persistence protocols and their SQLAlchemy implementation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastclaw.storage.models import (
    AgentFileModel,
    AgentModel,
    APIKeyAgentModel,
    APIKeyModel,
    ConfigModel,
    CronJobModel,
    SessionModel,
    UserModel,
    WebSessionModel,
)
from fastclaw.storage.records import (
    AgentFileRecord,
    AgentRecord,
    APIKeyRecord,
    ConfigRecord,
    CronJobRecord,
    SessionRecord,
    UserRecord,
    WebSessionRecord,
)


class UserRepository(Protocol):
    async def save_user(self, record: UserRecord) -> None: ...
    async def get_user(self, user_id: str) -> UserRecord | None: ...
    async def get_user_by_login(self, login: str) -> UserRecord | None: ...
    async def list_users(self) -> Sequence[UserRecord]: ...


class AgentRepository(Protocol):
    async def save_agent(self, record: AgentRecord) -> None: ...
    async def get_agent(self, agent_id: str) -> AgentRecord | None: ...
    async def list_agents(self, user_id: str) -> Sequence[AgentRecord]: ...


class SessionRepository(Protocol):
    async def save_session(self, record: SessionRecord) -> None: ...
    async def get_session(self, user_id: str, agent_id: str, key: str) -> SessionRecord | None: ...
    async def list_sessions(self, user_id: str, agent_id: str) -> Sequence[SessionRecord]: ...


class APIKeyRepository(Protocol):
    async def save_api_key(self, record: APIKeyRecord) -> None: ...
    async def get_api_key_by_hash(self, key_hash: str) -> APIKeyRecord | None: ...
    async def set_api_key_agents(self, api_key_id: str, agent_ids: Sequence[str]) -> None: ...
    async def api_key_can_access_agent(self, api_key_id: str, agent_id: str) -> bool: ...


class ConfigRepository(Protocol):
    async def save_config(self, record: ConfigRecord) -> None: ...
    async def list_configs(
        self, *, kind: str, user_id: str, agent_id: str
    ) -> Sequence[ConfigRecord]: ...


class SQLAlchemyStore:
    """Repository implementation bound to one caller-owned transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save_user(self, record: UserRecord) -> None:
        await self.session.merge(UserModel(**record.model_dump()))

    async def get_user(self, user_id: str) -> UserRecord | None:
        model = await self.session.get(UserModel, user_id)
        return self._user_record(model) if model is not None else None

    async def get_user_by_login(self, login: str) -> UserRecord | None:
        model = await self.session.scalar(
            select(UserModel).where(or_(UserModel.username == login, UserModel.email == login))
        )
        return self._user_record(model) if model is not None else None

    async def list_users(self) -> Sequence[UserRecord]:
        models = (await self.session.scalars(select(UserModel).order_by(UserModel.id))).all()
        return [self._user_record(model) for model in models]

    async def save_web_session(self, record: WebSessionRecord) -> None:
        await self.session.merge(WebSessionModel(**record.model_dump()))

    async def get_web_session(self, sid: str) -> WebSessionRecord | None:
        model = await self.session.get(WebSessionModel, sid)
        if model is None:
            return None
        return WebSessionRecord(
            sid=model.sid,
            user_id=model.user_id,
            created_at=model.created_at,
            expires_at=model.expires_at,
        )

    async def save_api_key(self, record: APIKeyRecord) -> None:
        await self.session.merge(APIKeyModel(**record.model_dump()))

    async def get_api_key_by_hash(self, key_hash: str) -> APIKeyRecord | None:
        model = await self.session.scalar(
            select(APIKeyModel).where(APIKeyModel.key_hash == key_hash)
        )
        return self._api_key_record(model) if model is not None else None

    async def set_api_key_agents(self, api_key_id: str, agent_ids: Sequence[str]) -> None:
        await self.session.execute(
            delete(APIKeyAgentModel).where(APIKeyAgentModel.apikey_id == api_key_id)
        )
        self.session.add_all(
            APIKeyAgentModel(apikey_id=api_key_id, agent_id=agent_id)
            for agent_id in dict.fromkeys(agent_ids)
        )

    async def api_key_can_access_agent(self, api_key_id: str, agent_id: str) -> bool:
        match = await self.session.scalar(
            select(APIKeyAgentModel.apikey_id).where(
                APIKeyAgentModel.apikey_id == api_key_id,
                APIKeyAgentModel.agent_id == agent_id,
            )
        )
        return match is not None

    async def save_agent(self, record: AgentRecord) -> None:
        await self.session.merge(AgentModel(**record.model_dump()))

    async def get_agent(self, agent_id: str) -> AgentRecord | None:
        model = await self.session.get(AgentModel, agent_id)
        return self._agent_record(model) if model is not None else None

    async def list_agents(self, user_id: str) -> Sequence[AgentRecord]:
        models = (
            await self.session.scalars(
                select(AgentModel).where(AgentModel.user_id == user_id).order_by(AgentModel.id)
            )
        ).all()
        return [self._agent_record(model) for model in models]

    async def save_session(self, record: SessionRecord) -> None:
        await self.session.merge(SessionModel(**record.model_dump()))

    async def get_session(self, user_id: str, agent_id: str, key: str) -> SessionRecord | None:
        model = await self.session.get(SessionModel, (user_id, agent_id, key))
        return self._session_record(model) if model is not None else None

    async def list_sessions(self, user_id: str, agent_id: str) -> Sequence[SessionRecord]:
        models = (
            await self.session.scalars(
                select(SessionModel)
                .where(SessionModel.user_id == user_id, SessionModel.agent_id == agent_id)
                .order_by(SessionModel.updated_at.desc())
            )
        ).all()
        return [self._session_record(model) for model in models]

    async def save_agent_file(self, record: AgentFileRecord) -> None:
        await self.session.merge(AgentFileModel(**record.model_dump()))

    async def list_agent_files(self, agent_id: str, user_id: str) -> Sequence[AgentFileRecord]:
        models = (
            await self.session.scalars(
                select(AgentFileModel)
                .where(
                    AgentFileModel.agent_id == agent_id,
                    AgentFileModel.user_id == user_id,
                )
                .order_by(AgentFileModel.filename)
            )
        ).all()
        return [
            AgentFileRecord(
                agent_id=model.agent_id,
                user_id=model.user_id,
                filename=model.filename,
                data=model.data,
                updated_at=model.updated_at,
            )
            for model in models
        ]

    async def save_config(self, record: ConfigRecord) -> None:
        await self.session.merge(ConfigModel(**record.model_dump()))

    async def list_configs(
        self, *, kind: str, user_id: str, agent_id: str
    ) -> Sequence[ConfigRecord]:
        if agent_id:
            scope_filter = (ConfigModel.scope == "agent", ConfigModel.scope_id == agent_id)
        elif user_id:
            scope_filter = (ConfigModel.scope == "user", ConfigModel.scope_id == user_id)
        else:
            scope_filter = (ConfigModel.scope == "system", ConfigModel.scope_id == "")
        models = (
            await self.session.scalars(
                select(ConfigModel)
                .where(
                    ConfigModel.kind == kind,
                    *scope_filter,
                )
                .order_by(ConfigModel.name)
            )
        ).all()
        return [self._config_record(model) for model in models]

    async def save_cron_job(self, record: CronJobRecord) -> None:
        await self.session.merge(CronJobModel(**record.model_dump()))

    @staticmethod
    def _user_record(model: UserModel) -> UserRecord:
        return UserRecord(
            id=model.id,
            username=model.username,
            email=model.email,
            password_hash=model.password_hash,
            display_name=model.display_name,
            role=model.role,
            status=model.status,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _api_key_record(model: APIKeyModel) -> APIKeyRecord:
        return APIKeyRecord(
            id=model.id,
            user_id=model.user_id,
            name=model.name,
            key_hash=model.key_hash,
            key_prefix=model.key_prefix,
            type=model.type,
            created_at=model.created_at,
        )

    @staticmethod
    def _agent_record(model: AgentModel) -> AgentRecord:
        return AgentRecord(
            id=model.id,
            user_id=model.user_id,
            name=model.name,
            config=model.config,
            is_public=model.is_public,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _session_record(model: SessionModel) -> SessionRecord:
        return SessionRecord(
            user_id=model.user_id,
            agent_id=model.agent_id,
            key=model.key,
            channel=model.channel,
            account_id=model.account_id,
            chat_id=model.chat_id,
            project_id=model.project_id,
            messages=model.messages,
            title=model.title,
            message_count=model.message_count,
            chatter_user_id=model.chatter_user_id,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _config_record(model: ConfigModel) -> ConfigRecord:
        return ConfigRecord(
            id=model.id,
            kind=model.kind,
            scope=model.scope,
            scope_id=model.scope_id,
            user_id=model.user_id,
            agent_id=model.agent_id,
            name=model.name,
            enabled=model.enabled,
            credential_key=model.credential_key,
            data=model.data,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )
