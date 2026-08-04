"""Credential compatibility and trusted request identity context."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass

import bcrypt


@dataclass(frozen=True, slots=True)
class Identity:
    user_id: str
    role: str
    auth_method: str
    api_key_id: str = ""
    api_key_agents: tuple[str, ...] = ()
    act_as_user_id: str = ""

    @property
    def effective_user_id(self) -> str:
        return self.act_as_user_id or self.user_id

    @property
    def read_only(self) -> bool:
        return bool(self.act_as_user_id and self.act_as_user_id != self.user_id)

    def can_access_agent(self, agent_id: str) -> bool:
        if self.role == "super_admin":
            return True
        if self.auth_method == "apikey":
            return agent_id in self.api_key_agents
        return True


_identity: ContextVar[Identity | None] = ContextVar("fastclaw_identity", default=None)


@contextmanager
def use_identity(identity: Identity) -> Iterator[Identity]:
    token: Token[Identity | None] = _identity.set(identity)
    try:
        yield identity
    finally:
        _identity.reset(token)


def current_identity() -> Identity | None:
    return _identity.get()


def require_identity() -> Identity:
    identity = current_identity()
    if identity is None:
        raise RuntimeError("trusted request identity is missing")
    return identity


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def generate_api_key() -> str:
    return f"fc_{secrets.token_urlsafe(32)}"


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
