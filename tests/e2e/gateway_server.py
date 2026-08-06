"""Start an isolated FastClaw Gateway for Playwright."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    repository = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="fastclaw-e2e-") as directory:
        root = Path(directory)
        os.environ.update(
            {
                "FASTCLAW_DATABASE_URL": f"sqlite+aiosqlite:///{root / 'fastclaw.db'}",
                "FASTCLAW_DATA_ROOT": str(root),
                "FASTCLAW_LEGACY_DATA_ROOT": str(root / "legacy-not-present"),
                "FASTCLAW_WEB_ROOT": str(repository / "web" / "out"),
                "FASTCLAW_PROVIDER_NAME": "fixture",
                "FASTCLAW_PROVIDER_API_KEY": "fixture-secret",
                "FASTCLAW_PROVIDER_API_BASE": "http://127.0.0.1:19001/v1",
                "FASTCLAW_PROVIDER_API_TYPE": "openai-compatible",
                "FASTCLAW_DEFAULT_MODEL": "fixture/model-1",
            }
        )
        uvicorn.run("fastclaw.app:create_app", factory=True, host="127.0.0.1", port=19000)
