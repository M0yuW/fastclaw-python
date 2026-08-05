"""Environment-backed gateway settings without hidden global mutation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    database_url: str
    data_root: Path = Path.home() / ".fastclaw-python"
    port: int = 18954
    session_ttl_seconds: int = 60 * 60 * 24 * 30
    secure_cookies: bool = False
    provider_name: str = ""
    provider_api_key: str = ""
    provider_api_base: str = ""
    provider_api_type: str = "openai-compatible"
    default_model: str = ""
    web_root: Path | None = None

    @classmethod
    def from_env(cls) -> GatewaySettings:
        default_db = Path.home() / ".fastclaw-python" / "fastclaw.db"
        checkout_web = Path(__file__).resolve().parents[3] / "web" / "out"
        configured_web = os.environ.get("FASTCLAW_WEB_ROOT")
        web_root = Path(configured_web).expanduser() if configured_web else checkout_web
        return cls(
            database_url=os.environ.get(
                "FASTCLAW_DATABASE_URL", f"sqlite+aiosqlite:///{default_db}"
            ),
            data_root=Path(
                os.environ.get("FASTCLAW_DATA_ROOT", str(Path.home() / ".fastclaw-python"))
            ).expanduser(),
            port=int(os.environ.get("FASTCLAW_PORT", "18954")),
            session_ttl_seconds=int(
                os.environ.get("FASTCLAW_SESSION_TTL_SECONDS", str(60 * 60 * 24 * 30))
            ),
            secure_cookies=os.environ.get("FASTCLAW_SECURE_COOKIES", "").lower()
            in {"1", "true", "yes"},
            provider_name=os.environ.get("FASTCLAW_PROVIDER_NAME", ""),
            provider_api_key=os.environ.get("FASTCLAW_PROVIDER_API_KEY", ""),
            provider_api_base=os.environ.get("FASTCLAW_PROVIDER_API_BASE", ""),
            provider_api_type=os.environ.get("FASTCLAW_PROVIDER_API_TYPE", "openai-compatible"),
            default_model=os.environ.get("FASTCLAW_DEFAULT_MODEL", ""),
            web_root=web_root if web_root.is_dir() else None,
        )
