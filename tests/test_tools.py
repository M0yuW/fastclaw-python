from __future__ import annotations

import json
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import anyio
import httpcore
import httpx
import pytest

from fastclaw.execution import ExecutionContext
from fastclaw.network import (
    PinnedAsyncHTTPTransport,
    PinnedNetworkBackend,
    pinned_network_target,
)
from fastclaw.tools import (
    FOOTBALL_COMPETITIONS,
    ExecTool,
    FootballDataTool,
    FootballLedgerTool,
    ListDirTool,
    ReadFileTool,
    ToolRegistry,
    ToolResult,
    WebFetchTool,
    WorldCupLedgerTool,
    WriteFileTool,
    resolve_football_competition,
)
from fastclaw.tools.football_data import EspnPublicFetcher


class FixtureFetcher:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    async def execute(
        self, arguments: dict[str, object], execution: ExecutionContext
    ) -> ToolResult:
        del execution
        url = str(arguments["url"])
        self.urls.append(url)
        for marker, payload in self.responses.items():
            if marker in url:
                return ToolResult(content=json.dumps(payload))
        return ToolResult(content="fixture URL not found", is_error=True)


def context() -> ExecutionContext:
    return ExecutionContext(
        user_id="user-1",
        agent_id="agent-1",
        session_id="session-1",
        root_execution_id="run-1",
    )


@pytest.mark.asyncio
async def test_espn_fetcher_rejects_non_fixed_origin_without_starting_process() -> None:
    result = await EspnPublicFetcher().execute({"url": "https://example.com/scoreboard"}, context())

    assert result.is_error
    assert "fixed soccer API origin" in result.content


def test_football_competition_catalog_resolves_reviewed_aliases() -> None:
    sweden = resolve_football_competition("瑞典超", country="瑞典")
    champions_league = resolve_football_competition("UCL", country="Europe")

    assert sweden is not None and sweden.espn_slug == "swe.1"
    assert champions_league is not None
    assert champions_league.espn_slug == "uefa.champions"
    assert len({item.espn_slug for item in FOOTBALL_COMPETITIONS}) == len(FOOTBALL_COMPETITIONS)
    assert resolve_football_competition("瑞典超", country="挪威") is None


@pytest.mark.asyncio
async def test_football_data_exposes_only_trusted_provider_mapping() -> None:
    fetcher = FixtureFetcher({})
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher)

    resolved = await tool.execute(
        {"action": "competition_resolve", "competition": "瑞典超级联赛"}, context()
    )
    rejected = await tool.execute(
        {"action": "competition_resolve", "competition": "swe.2"}, context()
    )

    assert json.loads(resolved.content)["mapping"] == {
        "key": "swedish-allsvenskan",
        "competition": "Swedish Allsvenskan",
        "country": "Sweden",
        "espn_slug": "swe.1",
    }
    assert rejected.is_error
    assert fetcher.urls == []


@pytest.mark.asyncio
async def test_football_data_espn_queries_use_and_validate_trusted_mapping() -> None:
    fetcher = FixtureFetcher(
        {
            "scoreboard": {
                "leagues": [{"slug": "swe.1", "name": "Swedish Allsvenskan"}],
                "events": [
                    {
                        "id": "401842783",
                        "name": "IF Brommapojkarna at IK Sirius",
                        "date": "2026-08-10T17:00Z",
                        "status": {"type": {"description": "Scheduled"}},
                        "competitions": [
                            {
                                "competitors": [
                                    {
                                        "homeAway": "home",
                                        "team": {"id": "1", "displayName": "IK Sirius"},
                                    },
                                    {
                                        "homeAway": "away",
                                        "team": {
                                            "id": "2",
                                            "displayName": "IF Brommapojkarna",
                                        },
                                    },
                                ]
                            }
                        ],
                    }
                ],
            },
            "summary": {
                "header": {
                    "league": {"slug": "swe.1"},
                    "competitions": [
                        {
                            "date": "2026-08-10T17:00Z",
                            "status": {"type": {"description": "Scheduled"}},
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "team": {"id": "1", "displayName": "IK Sirius"},
                                },
                                {
                                    "homeAway": "away",
                                    "team": {
                                        "id": "2",
                                        "displayName": "IF Brommapojkarna",
                                    },
                                },
                            ],
                        }
                    ],
                }
            },
        }
    )
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher)

    schedule = await tool.execute(
        {
            "action": "espn_schedule",
            "competition": "瑞典超",
            "date": "2026-08-10",
        },
        context(),
    )
    summary = await tool.execute(
        {
            "action": "espn_summary",
            "competition": "瑞典超",
            "event_id": "401842783",
        },
        context(),
    )

    assert json.loads(schedule.content)["matches"][0]["teams"]["home"]["name"] == ("IK Sirius")
    assert json.loads(summary.content)["mapping"]["espn_slug"] == "swe.1"
    assert "/swe.1/scoreboard?dates=20260810" in fetcher.urls[0]
    assert "/swe.1/summary?event=401842783" in fetcher.urls[1]


@pytest.mark.asyncio
async def test_football_data_espn_rejects_mismatched_response_league() -> None:
    fetcher = FixtureFetcher({"scoreboard": {"leagues": [{"slug": "fifa.world"}], "events": []}})

    result = await FootballDataTool(fetcher, espn_fetcher=fetcher).execute(
        {
            "action": "espn_schedule",
            "competition": "瑞典超",
            "date": "2026-08-10",
        },
        context(),
    )

    assert result.is_error
    assert "does not match" in json.loads(result.content)["error"]


@pytest.mark.asyncio
async def test_football_data_resolves_non_world_cup_competition_and_schedule() -> None:
    fetcher = FixtureFetcher(
        {
            "search_all_leagues.php": {
                "countries": [
                    {
                        "idLeague": "4429",
                        "strLeague": "FIFA World Cup",
                        "strLeagueAlternate": "",
                        "strCountry": "Worldwide",
                    },
                    {
                        "idLeague": "4613",
                        "strLeague": "Swedish Allsvenskan",
                        "strLeagueAlternate": "Allsvenskan",
                        "strCountry": "Sweden",
                    },
                ]
            },
            "eventsday.php": {"events": [{"strEvent": "IK Sirius vs IF Brommapojkarna"}]},
        }
    )
    tool = FootballDataTool(fetcher)

    resolved = await tool.execute(
        {
            "action": "competition_search",
            "competition": "Allsvenskan",
            "country": "Sweden",
        },
        context(),
    )
    scheduled = await tool.execute(
        {
            "action": "schedule",
            "competition": "Allsvenskan",
            "country": "Sweden",
            "date": "2026-08-10",
        },
        context(),
    )

    assert json.loads(resolved.content)["competitions"] == [
        {
            "league_id": "4613",
            "competition": "Swedish Allsvenskan",
            "alternate": "Allsvenskan",
            "country": "Sweden",
        }
    ]
    assert json.loads(scheduled.content)["rows"][0]["match"] == ("IK Sirius vs IF Brommapojkarna")
    assert "l=4613" in fetcher.urls[-1]
    assert "c=Sweden" in fetcher.urls[0]


@pytest.mark.asyncio
async def test_football_data_sporttery_match_is_ev_only() -> None:
    fetcher = FixtureFetcher(
        {
            "getMatchCalculatorV1": {
                "value": {
                    "matchInfoList": [
                        {
                            "subMatchList": [
                                {
                                    "matchId": "swe-1",
                                    "leagueAllName": "瑞典超级联赛",
                                    "homeTeamAllName": "天狼星",
                                    "homeTeamAbbEnName": "IK Sirius",
                                    "awayTeamAllName": "布洛马波卡纳",
                                    "awayTeamAbbEnName": "IF Brommapojkarna",
                                    "had": {"h": "2.10", "d": "3.20", "a": "3.05"},
                                }
                            ]
                        }
                    ]
                }
            }
        }
    )

    result = await FootballDataTool(fetcher, allow_sporttery=True).execute(
        {
            "action": "sporttery_match",
            "team_a": "Sirius",
            "team_b": "Brommapojkarna",
        },
        context(),
    )

    payload = json.loads(result.content)
    assert payload["competition"] == "瑞典超级联赛"
    assert payload["match_id"] == "swe-1"


@pytest.mark.asyncio
async def test_football_data_rejects_sporttery_for_non_ev_roles() -> None:
    fetcher = FixtureFetcher({})

    result = await FootballDataTool(fetcher).execute(
        {
            "action": "sporttery_match",
            "team_a": "Sirius",
            "team_b": "Brommapojkarna",
        },
        context(),
    )

    assert result.is_error
    assert "EV analyst role" in result.content
    assert fetcher.urls == []


@pytest.mark.asyncio
async def test_read_file_is_confined_to_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fixture = workspace / "data.txt"
    fixture.write_text("safe", encoding="utf-8")
    registry = ToolRegistry([ReadFileTool(workspace)])

    result = await registry.execute("read_file", {"path": "data.txt"}, context())
    denied = await registry.execute("read_file", {"path": "../outside"}, context())

    assert result.content == "safe"
    assert denied.is_error
    assert "tool 'read_file' failed (reference " in denied.content
    assert "outside" not in denied.content


async def test_workspace_list_and_atomic_write_remain_confined(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    writer = WriteFileTool(workspace)
    listing = ListDirTool(workspace)

    written = await writer.execute({"path": "reports/result.txt", "content": "complete"}, context())
    result = await listing.execute({"path": "reports"}, context())

    assert written.content == "wrote 8 bytes"
    assert result.content == '[{"name":"result.txt","type":"file"}]'
    with pytest.raises(ValueError, match="workspace"):
        await writer.execute({"path": "../../escape", "content": "no"}, context())


async def test_worldcup_ledger_is_atomic_unique_and_reports_directly(tmp_path: Path) -> None:
    tool = WorldCupLedgerTool(tmp_path)
    entry = {
        "date": "2026-06-30",
        "match": "France vs Sweden",
        "our_pred": "France",
        "our_confidence": "high",
        "actual_result": None,
    }

    appended = await tool.execute({"operation": "append", "entry": entry}, context())
    with pytest.raises(ValueError, match="already contains"):
        await tool.execute({"operation": "append", "entry": entry}, context())
    settled = await tool.execute(
        {
            "operation": "settle",
            "date": entry["date"],
            "match": entry["match"],
            "actual_result": "France",
            "actual_score": "2-1",
        },
        context(),
    )
    report = await tool.execute({"operation": "report"}, context())

    assert appended.content == "prediction appended"
    assert settled.content == "prediction settled"
    assert report.direct_return is True
    assert "France vs Sweden" in report.content
    assert "2-1" in report.content


async def test_football_ledger_scopes_entries_by_competition(tmp_path: Path) -> None:
    tool = FootballLedgerTool(tmp_path)
    premier_league = {
        "competition": "Premier League",
        "season": "2026/27",
        "date": "2026-08-15",
        "match": "Team A vs Team B",
        "our_pred": "Team A",
        "our_confidence": "mid",
        "actual_result": None,
    }
    fa_cup = {**premier_league, "competition": "FA Cup", "season": "2026/27"}

    await tool.execute({"operation": "append", "entry": premier_league}, context())
    await tool.execute({"operation": "append", "entry": fa_cup}, context())
    with pytest.raises(ValueError, match="already contains"):
        await tool.execute(
            {
                "operation": "append",
                "entry": {
                    **premier_league,
                    "competition": " premier  league ",
                    "match": "team a VS team b",
                },
            },
            context(),
        )

    await tool.execute(
        {
            "operation": "settle",
            "competition": "Premier League",
            "date": "2026-08-15",
            "match": "Team A vs Team B",
            "actual_result": "draw",
            "actual_score": "1-1",
        },
        context(),
    )
    premier_report = await tool.execute(
        {"operation": "report", "competition": "Premier League"}, context()
    )
    pending_report = await tool.execute({"operation": "report", "pending_only": True}, context())

    assert premier_report.direct_return is True
    assert "Premier League" in premier_report.content
    assert "FA Cup" not in premier_report.content
    assert "1-1" in premier_report.content
    assert "FA Cup" in pending_report.content
    assert "Premier League" not in pending_report.content
    assert (tmp_path / "workspaces" / "agent-1" / "football" / "ledger.json").is_file()


@pytest.mark.asyncio
async def test_tool_policy_and_web_scheme_are_enforced() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="fixture", request=request)

    async def public_resolver(host: str, port: int) -> Sequence[str]:
        del host, port
        return ("93.184.216.34",)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry = ToolRegistry([WebFetchTool(client, resolver=public_resolver)])
        denied = await registry.execute(
            "web_fetch",
            {"url": "https://example.test/data"},
            context(),
            allowed=frozenset(),
        )
        bad_scheme = await registry.execute("web_fetch", {"url": "file:///etc/passwd"}, context())
        fetched = await registry.execute(
            "web_fetch", {"url": "https://example.test/data"}, context()
        )

    assert denied.is_error
    assert bad_scheme.is_error
    assert fetched.content == "fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "http://[::1]/private",
        "http://169.254.169.254/latest/meta-data",
        "https://user:password@example.test/private",
    ],
)
async def test_web_fetch_rejects_non_public_and_credentialed_urls(url: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"denied URL reached transport: {request.url}")

    async def resolver(host: str, port: int) -> Sequence[str]:
        del host, port
        return ("127.0.0.1",)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WebFetchTool(client, resolver=resolver).execute({"url": url}, context())

    assert result.is_error


@pytest.mark.asyncio
async def test_web_fetch_revalidates_redirect_destinations() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/private"},
            request=request,
        )

    async def resolver(host: str, port: int) -> Sequence[str]:
        del host, port
        return ("93.184.216.34",)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WebFetchTool(client, resolver=resolver).execute(
            {"url": "https://public.example/start"}, context()
        )

    assert result.is_error
    assert len(requests) == 1
    assert "non-public" in result.content


class _FakeNetworkStream(httpcore.AsyncNetworkStream):
    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload
        self.written = bytearray()
        self.server_hostname: str | None = None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del timeout
        chunk = self.payload[:max_bytes]
        self.payload = self.payload[max_bytes:]
        return chunk

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del timeout
        self.written.extend(buffer)

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: object,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del ssl_context, timeout
        self.server_hostname = server_hostname
        return self


class _FakeNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        failing: frozenset[str] = frozenset(),
        payload: bytes = b"",
    ) -> None:
        self.failing = failing
        self.hosts: list[str] = []
        self.stream = _FakeNetworkStream(payload)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        del port, timeout, local_address, socket_options
        self.hosts.append(host)
        if host in self.failing:
            raise httpcore.ConnectError("fixture connection failed")
        return self.stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: object = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise AssertionError("Unix socket should not be used")

    async def sleep(self, seconds: float) -> None:
        del seconds


async def test_pinned_network_backend_never_resolves_the_hostname_again() -> None:
    delegate = _FakeNetworkBackend(failing=frozenset({"93.184.216.34"}))
    backend = PinnedNetworkBackend(delegate)

    with pinned_network_target(
        "public.example",
        443,
        ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
    ):
        stream = await backend.connect_tcp("public.example", 443)

    assert stream is delegate.stream
    assert delegate.hosts == ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]
    with pytest.raises(httpcore.ConnectError, match="unpinned"):
        await backend.connect_tcp("public.example", 443)


async def test_pinned_transport_preserves_host_and_tls_sni() -> None:
    delegate = _FakeNetworkBackend(payload=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    transport = PinnedAsyncHTTPTransport(delegate)

    async with httpx.AsyncClient(transport=transport) as client:
        with pinned_network_target("public.example", 443, ("93.184.216.34",)):
            response = await client.get("https://public.example/report")

    assert response.text == "ok"
    assert delegate.hosts == ["93.184.216.34"]
    assert delegate.stream.server_hostname == "public.example"
    assert b"Host: public.example\r\n" in delegate.stream.written


@pytest.mark.asyncio
async def test_exec_uses_pinned_absolute_executable_and_denies_argv_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    echo = shutil.which("echo")
    assert echo is not None
    tool = ExecTool(
        workspace,
        allowed_executables={"echo": Path(echo)},
        writable_roots=(),
    )

    result = await tool.execute({"argv": ["echo", "safe"]}, context())
    denied = await tool.execute({"argv": [str(workspace / "echo"), "unsafe"]}, context())

    assert result.content.strip() == "safe"
    assert not result.is_error
    assert denied.is_error
    assert "paths are denied" in denied.content


@pytest.mark.asyncio
async def test_exec_rejects_an_executable_replaced_after_validation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "trusted-echo"
    replacement = tmp_path / "replacement"
    source = shutil.which("echo")
    assert source is not None
    shutil.copy(source, executable)
    shutil.copy(source, replacement)
    tool = ExecTool(
        workspace,
        allowed_executables={"echo": executable},
        writable_roots=(),
    )
    os.replace(replacement, executable)

    result = await tool.execute({"argv": ["echo", "unsafe"]}, context())

    assert result.is_error
    assert "changed after policy validation" in result.content


@pytest.mark.asyncio
async def test_exec_bounds_output_and_kills_a_term_ignoring_process(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shell = shutil.which("sh")
    assert shell is not None
    tool = ExecTool(
        workspace,
        allowed_executables={"sh": Path(shell)},
        writable_roots=(),
        max_output_bytes=64,
        termination_grace_seconds=0.05,
    )
    started = anyio.current_time()

    result = await tool.execute(
        {"argv": ["sh", "-c", "trap '' TERM; while :; do printf xxxxxxxxxxxxxxxx; done"]},
        context(),
    )

    assert len(result.content.encode()) == 64
    assert result.metadata["truncated"] is True
    assert anyio.current_time() - started < 1


@pytest.mark.asyncio
async def test_exec_cancellation_kills_the_entire_process_group(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shell = shutil.which("sh")
    assert shell is not None
    tool = ExecTool(
        workspace,
        allowed_executables={"sh": Path(shell)},
        writable_roots=(),
        termination_grace_seconds=0.05,
    )
    started = anyio.current_time()
    process_group_file = workspace / "process-group"

    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.05):
            await tool.execute(
                {
                    "argv": [
                        "sh",
                        "-c",
                        (
                            "printf '%s' $$ > process-group; "
                            "trap '' TERM; (trap '' TERM; while :; do :; done) & wait"
                        ),
                    ]
                },
                context(),
            )

    assert anyio.current_time() - started < 1
    process_group_id = int(process_group_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.killpg(process_group_id, 0)
