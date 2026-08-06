#!/usr/bin/env python3
"""Deterministic OpenAI-compatible Provider for the locked 27-Agent wiring smoke."""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SCENARIOS = (
    (
        "# Soul — 世界杯预测总控",
        (
            "agt_3372d8716415f15b50cc",
            "agt_2dc40bf88b2398422edd",
            "agt_5909ce07c02eea3c535a",
            "agt_b3925b25aab7705a0f85",
            "agt_7bbaaf3aced233f3a329",
            "agt_70b89a987f1a6f6c2383",
        ),
        True,
    ),
    (
        "# Soul — 总控 Agent",
        ("agt_4e85c80c71dc538a1139", "agt_5bb1e62935afc80d77ce"),
        False,
    ),
    (
        "# Finance Research Coordinator",
        (
            "finance-accounting",
            "finance-governance",
            "finance-methodology",
            "finance-retriever",
            "finance-risk",
        ),
        False,
    ),
    (
        "# Runtime Benchmark Coordinator",
        ("bench-investigator", "bench-observer", "bench-operator", "bench-policy"),
        False,
    ),
)


def _event(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        messages = request.get("messages") or []
        system = "\n".join(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "system"
        )
        user_prompt = "\n".join(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "user"
        )
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        scenario = next((item for item in SCENARIOS if item[0] in system), None)
        if "authenticated tool differential fixture" in user_prompt:
            # Locked Go and Python assemble role files into different system
            # prompt layouts. The explicit fixture marker selects the same
            # coordinator behavior without weakening production matching.
            scenario = SCENARIOS[2]

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()

        if "cancel-wiring-smoke" in user_prompt:
            self._slow_stream()
            return
        payload = self._next_payload(scenario, tool_messages)
        self.wfile.write(_event(payload))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _slow_stream(self) -> None:
        try:
            for index in range(100):
                self.wfile.write(
                    _event(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": f"partial-{index} "},
                                    "finish_reason": None,
                                }
                            ]
                        }
                    )
                )
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            return

    @staticmethod
    def _next_payload(
        scenario: tuple[str, tuple[str, ...], bool] | None,
        tool_messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if scenario is None:
            return {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "specialist fixture result"},
                        "finish_reason": "stop",
                    }
                ]
            }
        _, targets, direct_ledger = scenario
        delegated = sum(message.get("name") == "spawn_subagent" for message in tool_messages)
        if delegated < len(targets):
            return _tool_payload(
                f"call-{delegated}",
                "spawn_subagent",
                {"agent_id": targets[delegated], "task": f"fixture delegation {delegated}"},
            )
        if direct_ledger:
            return _tool_payload("call-ledger", "worldcup_ledger", {"operation": "report"})
        return {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": f"wiring smoke complete: {delegated} results"},
                    "finish_reason": "stop",
                }
            ]
        }

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _tool_payload(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19002)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), FixtureHandler).serve_forever()


if __name__ == "__main__":
    main()
