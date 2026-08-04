import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from fastclaw.providers import ChatMessage

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "go792"


def test_locked_go_fixture_contains_replayable_tool_session() -> None:
    connection = sqlite3.connect(FIXTURE_ROOT / "fastclaw-go.db")
    try:
        row = connection.execute(
            "SELECT messages FROM sessions WHERE session_key = ?", ("web_go_fixture",)
        ).fetchone()
    finally:
        connection.close()

    assert row is not None
    messages = json.loads(row[0])
    assistant = ChatMessage.model_validate(messages[1])
    assert assistant.tool_calls[0].id == "tool-go-1"
    assert assistant.thinking == "fixture reasoning"
    assert assistant.raw_assistant is not None
    content = cast(list[dict[str, Any]], assistant.raw_assistant["content"])
    assert content[0]["signature"] == "fixture-signature"


def test_locked_go_fixture_has_cross_language_identity_and_channel_rows() -> None:
    connection = sqlite3.connect(FIXTURE_ROOT / "fastclaw-go.db")
    try:
        api_key = connection.execute(
            "SELECT key_hash FROM apikeys WHERE id = ?", ("k_go_fixture",)
        ).fetchone()
        acl = connection.execute(
            "SELECT agent_id FROM apikey_agents WHERE apikey_id = ?", ("k_go_fixture",)
        ).fetchone()
        channel = connection.execute(
            "SELECT credential_key, data FROM configs WHERE id = ?", ("cfg_go_channel",)
        ).fetchone()
    finally:
        connection.close()

    assert api_key is not None and len(api_key[0]) == 64
    assert acl == ("agt_go_fixture",)
    assert channel is not None and channel[0] == "fixture-tail"
    assert json.loads(channel[1])["botToken"] == "fixture-bot-token"
