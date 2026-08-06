"""Discovery, lifecycle, and trusted tool adapters for JSON-RPC plugins."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from fastclaw.execution import ExecutionContext
from fastclaw.plugin.models import (
    PluginError,
    PluginManifest,
    PluginProtocolError,
    PluginToolDefinition,
    PluginUnavailableError,
)
from fastclaw.plugin.process import PluginProcess
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools import ToolResult

logger = logging.getLogger(__name__)

_TRUSTED_ARGUMENT_NAMES = {
    "userId",
    "user_id",
    "agentId",
    "agent_id",
    "sessionId",
    "session_id",
    "rootExecutionId",
    "root_execution_id",
    "callPath",
    "call_path",
}


@dataclass(slots=True)
class PluginInstance:
    manifest: PluginManifest
    config: dict[str, Any]
    enabled: bool
    process: PluginProcess
    tools: tuple[PluginToolDefinition, ...] = ()
    error: str = ""
    restart_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PluginTool:
    def __init__(self, manager: PluginManager, plugin_id: str, tool: PluginToolDefinition) -> None:
        self.manager = manager
        self.plugin_id = plugin_id
        self.tool = tool
        self._definition = ToolDefinition(
            function=ToolFunction(
                name=f"{plugin_id}.{tool.name}",
                description=tool.description,
                parameters=tool.parameters,
            )
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        forbidden = sorted(_TRUSTED_ARGUMENT_NAMES & arguments.keys())
        if forbidden:
            return ToolResult(
                content="plugin identity fields are Runtime-managed and cannot be supplied",
                is_error=True,
            )
        try:
            result = await self.manager.execute(
                self.plugin_id,
                self.tool.name,
                arguments,
                context,
            )
        except PluginError as exc:
            return ToolResult(content=self.manager.safe_error(exc), is_error=True)
        content = self.manager.sanitize(str(result.get("result") or ""))
        is_error = False
        try:
            envelope = json.loads(content)
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict) and envelope.get("ok") is False:
            is_error = True
        return ToolResult(content=content, is_error=is_error)


class PluginManager:
    def __init__(
        self,
        roots: tuple[Path, ...],
        *,
        data_root: Path,
        configurations: dict[str, dict[str, Any]] | None = None,
        environments: dict[str, dict[str, str]] | None = None,
        enabled: set[str] | None = None,
    ) -> None:
        self.roots = tuple(root.expanduser().resolve() for root in roots)
        self.data_root = data_root.expanduser().resolve()
        self.configurations = configurations or {}
        self.environments = environments or {}
        self.enabled = enabled
        self._instances: dict[str, PluginInstance] = {}
        self._closing = False

    @property
    def instances(self) -> tuple[PluginInstance, ...]:
        return tuple(self._instances[key] for key in sorted(self._instances))

    def discover(self) -> tuple[PluginInstance, ...]:
        discovered: dict[str, PluginInstance] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for manifest_path in sorted(root.glob("*/plugin.json")):
                plugin_root = manifest_path.parent.resolve()
                if not plugin_root.is_relative_to(root) or manifest_path.is_symlink():
                    continue
                try:
                    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest = PluginManifest.model_validate({**raw, "root": plugin_root})
                except (OSError, json.JSONDecodeError, ValidationError) as exc:
                    raise PluginProtocolError(
                        f"invalid plugin manifest {manifest_path.name}"
                    ) from exc
                if manifest.id in discovered:
                    raise PluginProtocolError(f"duplicate plugin id {manifest.id!r}")
                enabled = self.enabled is None or manifest.id in self.enabled
                config = dict(self.configurations.get(manifest.id, {}))
                discovered[manifest.id] = PluginInstance(
                    manifest=manifest,
                    config=config,
                    enabled=enabled,
                    process=PluginProcess(
                        manifest,
                        config=config,
                        data_root=self.data_root,
                        environment=self.environments.get(manifest.id),
                        call_timeout=float(config.get("timeoutSeconds") or 45),
                    ),
                )
        self._instances = discovered
        return self.instances

    async def start(self) -> None:
        self._closing = False
        for instance in self.instances:
            if not instance.enabled:
                continue
            await self._start_instance(instance)

    async def stop(self) -> None:
        if self._closing:
            return
        self._closing = True
        await asyncio.gather(
            *(instance.process.stop() for instance in reversed(self.instances)),
            return_exceptions=True,
        )

    def tools(self) -> tuple[PluginTool, ...]:
        return tuple(
            PluginTool(self, instance.manifest.id, tool)
            for instance in self.instances
            if instance.enabled and instance.process.running
            for tool in instance.tools
        )

    async def configure(
        self,
        plugin_id: str,
        *,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
        restart: bool = False,
    ) -> PluginInstance:
        instance = self._instances.get(plugin_id)
        if instance is None:
            raise LookupError("plugin not found")
        async with instance.restart_lock:
            if config is not None:
                instance.config = dict(config)
                instance.process.config = instance.config
                instance.process.call_timeout = float(config.get("timeoutSeconds") or 45)
                restart = True
            if enabled is False:
                instance.enabled = False
                await instance.process.stop()
                return instance
            if enabled is True:
                instance.enabled = True
                restart = True
            if restart and instance.enabled:
                await instance.process.stop()
                await self._start_instance(instance)
        return instance

    async def execute(
        self,
        plugin_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> dict[str, Any]:
        instance = self._instances.get(plugin_id)
        if instance is None or not instance.enabled or self._closing:
            raise PluginUnavailableError("plugin is unavailable")
        await self._ensure_running(instance)
        try:
            result = await instance.process.call(
                "tool.execute",
                {
                    "name": tool_name,
                    "args": arguments,
                    "context": {
                        "userId": context.user_id,
                        "agentId": context.agent_id,
                        "sessionId": context.session_id,
                        "rootExecutionId": context.root_execution_id,
                        "callPath": list(context.call_path),
                    },
                },
            )
        except PluginUnavailableError:
            # Re-establish the process for a future call, but never replay an
            # in-flight state mutation whose commit status is unknown.
            await self._restart(instance)
            raise
        if not isinstance(result, dict) or not isinstance(result.get("result"), str):
            raise PluginProtocolError("plugin tool response is malformed")
        return result

    async def _start_instance(self, instance: PluginInstance) -> None:
        try:
            await instance.process.start()
            result = await instance.process.call("tool.list", timeout_seconds=10.0)
            raw_tools = result.get("tools") if isinstance(result, dict) else None
            if not isinstance(raw_tools, list):
                raise PluginProtocolError("plugin tool.list response is malformed")
            instance.tools = tuple(PluginToolDefinition.model_validate(item) for item in raw_tools)
            instance.error = ""
        except (PluginError, ValidationError) as exc:
            instance.error = self.safe_error(exc)
            await instance.process.stop()
            logger.exception("plugin %s failed to start", instance.manifest.id)

    async def _ensure_running(self, instance: PluginInstance) -> None:
        if instance.process.running:
            return
        await self._restart(instance)
        if not instance.process.running:
            raise PluginUnavailableError("plugin restart failed")

    async def _restart(self, instance: PluginInstance) -> None:
        async with instance.restart_lock:
            if self._closing or instance.process.running:
                return
            await self._start_instance(instance)

    def sanitize(self, value: str) -> str:
        sanitized = value.replace(str(self.data_root), "[data]")
        for root in self.roots:
            sanitized = sanitized.replace(str(root), "[plugin]")
        return sanitized[:200_000]

    def safe_error(self, error: BaseException) -> str:
        if isinstance(error, PluginUnavailableError):
            return "plugin is temporarily unavailable"
        if isinstance(error, PluginProtocolError):
            return "plugin protocol error"
        return "plugin call failed"
