#!/usr/bin/env python3
"""Compare locked HTTP/SSE fixtures against independent Go and Python runtimes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx

from fastclaw.differential import DifferentialCase, run_case


def load_cases(path: Path) -> tuple[DifferentialCase, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        DifferentialCase(
            name=str(item["name"]),
            method=str(item.get("method") or "GET"),
            path=str(item["path"]),
            body=item.get("body"),
            stream=bool(item.get("stream")),
            comparison=str(item.get("comparison") or "json_shape"),
            equal_paths=tuple(str(value) for value in item.get("equalPaths", [])),
            require_terminal_tasks=bool(item.get("requireTerminalTasks")),
            headers=dict(item.get("headers") or {}),
        )
        for item in payload["cases"]
    )


async def compare(args: argparse.Namespace) -> list[dict[str, Any]]:
    cases = load_cases(args.fixture)
    async with (
        httpx.AsyncClient(base_url=args.go_base, timeout=args.timeout) as go_client,
        httpx.AsyncClient(base_url=args.python_base, timeout=args.timeout) as python_client,
    ):
        results = []
        for case in cases:
            results.append(
                await run_case(
                    go_client,
                    python_client,
                    case,
                    go_token=os.environ.get("FASTCLAW_DIFFERENTIAL_GO_TOKEN", ""),
                    python_token=os.environ.get("FASTCLAW_DIFFERENTIAL_PYTHON_TOKEN", ""),
                )
            )
        return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--go-base", default="http://127.0.0.1:18953")
    parser.add_argument("--python-base", default="http://127.0.0.1:18954")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/differential-smoke.json"),
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.dumps(asyncio.run(compare(args)), ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.write_text(report + "\n", encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
