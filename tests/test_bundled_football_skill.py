from __future__ import annotations

import importlib.util
import io
import sys
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from types import ModuleType

from pytest import MonkeyPatch

from fastclaw.tools import FOOTBALL_COMPETITIONS


def _load_espn_script() -> ModuleType:
    script = (
        Path(__file__).parents[1] / "skills" / "match-data-toolkit" / "scripts" / "espn_data.py"
    )
    spec = importlib.util.spec_from_file_location("bundled_espn_data", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bundled_espn_script_matches_runtime_competition_catalog() -> None:
    module = _load_espn_script()
    script_mappings = {
        (name, country, slug) for name, country, slug, _aliases in module.COMPETITIONS
    }
    runtime_mappings = {(item.name, item.country, item.espn_slug) for item in FOOTBALL_COMPETITIONS}

    assert script_mappings == runtime_mappings
    assert module.resolve_competition("瑞典超")["espn_slug"] == "swe.1"
    assert module.resolve_competition("swe.2") is None


def test_bundled_espn_script_uses_mapping_and_rejects_response_mismatch() -> None:
    module = _load_espn_script()
    mapping = module.resolve_competition("瑞典超")
    requested: list[str] = []

    def valid_get(url: str) -> dict[str, object]:
        requested.append(url)
        return {"leagues": [{"slug": "swe.1", "name": "Swedish Allsvenskan"}], "events": []}

    module.__dict__["_get"] = valid_get
    result = module.fetch_schedule("2026-08-10", mapping)

    assert result["mapping"]["espn_slug"] == "swe.1"
    assert "/swe.1/scoreboard?dates=20260810" in requested[0]

    module.__dict__["_get"] = lambda _url: {
        "leagues": [{"slug": "fifa.world"}],
        "events": [],
    }
    mismatch = module.fetch_schedule("2026-08-10", mapping)
    assert mismatch["error"] == "ESPN response competition does not match the trusted mapping"


def test_bundled_espn_script_uses_browser_compatible_request_headers(
    monkeypatch: MonkeyPatch,
) -> None:
    module = _load_espn_script()
    captured: dict[str, str] = {}

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b"{}"

    def urlopen(request: urllib.request.Request, timeout: int) -> Response:
        assert timeout == 25
        captured.update(dict(request.header_items()))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    module._get("https://site.api.espn.com/example")

    assert captured["Accept"] == "application/json,text/plain,*/*"
    assert "Mozilla/5.0" in captured["User-agent"]


def test_bundled_espn_script_falls_back_to_fixed_system_curl_on_403(
    monkeypatch: MonkeyPatch,
) -> None:
    module = _load_espn_script()
    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/swe.1/scoreboard"

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.HTTPError(url, 403, "Forbidden", Message(), io.BytesIO())

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    module.__dict__["_get_with_system_curl"] = lambda actual: {
        "source": actual,
        "leagues": [{"slug": "swe.1"}],
    }

    assert module._get(url)["source"] == url
