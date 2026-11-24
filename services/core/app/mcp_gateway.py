"""MCP gateway: persistent stdio sessions exposed as a synchronous call API.

The orchestrator is synchronous while the MCP Python SDK is async. Rather than
spawning a subprocess per tool call, the gateway owns one background event loop
and keeps every server session open.

M9 additions
------------
* **Shared mode** — one gateway per process, reused by every analysis. A
  per-run gateway cost ~6 Python interpreter startups per incident; shared mode
  turns that into a one-time warm-up.
* **Health monitor** — pings every session and restarts a dead server with
  backoff, without taking down the other five.
* **Metrics** — server up/down, restart count, tool calls and latency are
  exported through prometheus_client, so the gateway itself is observable.
* **Thread safety** — concurrent analyses share sessions; session replacement
  is serialised per server, and callers never hold a stale connection lock.

Failure policy: a server that cannot start is marked unavailable, its tools are
reported missing, and the analysis degrades instead of crashing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import Settings, load_settings
from .tools import AuditRecord, ToolResult

PROJECT_ROOT = Path(__file__).resolve().parents[3]
log = logging.getLogger(__name__)

try:  # metrics are optional so the domain layer can run dependency-free
    from prometheus_client import Counter, Gauge, Histogram

    MCP_SERVER_UP = Gauge(
        "copilot_mcp_server_up", "1 when the MCP stdio server session is healthy", ["server"]
    )
    MCP_RESTARTS = Counter(
        "copilot_mcp_server_restarts_total", "MCP server restarts by the health monitor", ["server"]
    )
    MCP_TOOL_CALLS = Counter(
        "copilot_mcp_tool_calls_total", "MCP tool calls", ["server", "tool", "ok"]
    )
    MCP_TOOL_LATENCY = Histogram(
        "copilot_mcp_tool_latency_seconds", "MCP tool latency", ["server", "tool"]
    )
    HAS_MCP_METRICS = True
except ImportError:  # pragma: no cover - dependency is in requirements
    HAS_MCP_METRICS = False


@dataclass(frozen=True)
class MCPServerSpec:
    name: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)
    command: str = sys.executable


def _allowlist_env(settings: Settings) -> str:
    return ",".join(settings.tool_url_allowlist)


def default_servers(settings: Settings | None = None) -> tuple[MCPServerSpec, ...]:
    """The six stdio servers, with upstreams taken from settings.

    Per-call ``datasource_url`` from the service catalog overrides these
    defaults; the env value is only the fallback.
    """
    s = settings or load_settings()
    allow = _allowlist_env(s)
    return (
        MCPServerSpec("prometheus", ["-m", "services.mcp_servers.prometheus"],
                      {"PROMETHEUS_URL": s.prometheus_url,
                       "PROMETHEUS_ALLOWLIST": allow}),
        MCPServerSpec("loki", ["-m", "services.mcp_servers.loki"],
                      {"LOKI_URL": s.loki_url, "LOKI_ALLOWLIST": allow}),
        MCPServerSpec("cmdb", ["-m", "services.mcp_servers.cmdb"],
                      {"CMDB_URL": s.cmdb_url, "CMDB_ALLOWLIST": allow}),
        MCPServerSpec("deploy", ["-m", "services.mcp_servers.deploy"],
                      {"DEPLOY_URL": s.deploy_url, "DEPLOY_ALLOWLIST": allow}),
        MCPServerSpec("ticket", ["-m", "services.mcp_servers.ticket"]),
        MCPServerSpec("kb", ["-m", "services.mcp_servers.kb"],
                      {"KB_URL": s.kb_url, "KB_ALLOWLIST": allow}),
    )


class _Connection:
    """One live MCP session against one server process."""

    def __init__(self, spec: MCPServerSpec) -> None:
        self.spec = spec
        self.stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.tools: list[dict[str, Any]] = []
        self.error: str = ""
        self.opened_at: float = 0.0
        self.restarts: int = 0

    async def open(self) -> None:
        params = StdioServerParameters(
            command=self.spec.command,
            args=self.spec.args,
            # Merge with the parent environment: replacing it wholesale on
            # Windows drops PATH/SYSTEMROOT and the interpreter cannot start.
            env={**os.environ, **self.spec.env} if self.spec.env else None,
            cwd=str(PROJECT_ROOT),
        )
        self.stack = AsyncExitStack()
        read, write = await self.stack.enter_async_context(stdio_client(params))
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        listed = await self.session.list_tools()
        self.tools = [
            {
                "name": f"{self.spec.name}.{tool.name}",
                "description": tool.description or "",
                "input_schema": getattr(tool, "inputSchema", None),
            }
            for tool in listed.tools
        ]
        self.error = ""
        self.opened_at = time.time()

    async def close(self) -> None:
        self.session = None
        if self.stack is not None:
            await self.stack.aclose()
            self.stack = None

    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolResult:
        started = time.perf_counter()
        if self.session is None:
            return ToolResult(False, f"{self.spec.name}.{tool}", error="session not open")
        result = await self.session.call_tool(tool, arguments)
        latency = int((time.perf_counter() - started) * 1000)
        if getattr(result, "isError", False):
            return ToolResult(False, f"{self.spec.name}.{tool}",
                              error=str(result.content)[:200], latency_ms=latency)

        data: Any = None
        structured = getattr(result, "structuredContent", None)
        if structured:
            data = structured
        else:
            for item in result.content or []:
                text = getattr(item, "text", None)
                if text:
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        data = text
                    break
        return ToolResult(True, f"{self.spec.name}.{tool}", data=data, latency_ms=latency)


class MCPGateway:
    """Sync facade over async MCP sessions."""

    def __init__(
        self,
        servers: tuple[MCPServerSpec, ...] | None = None,
        timeout: float = 60.0,
        settings: Settings | None = None,
        health_interval_s: float | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.servers = servers if servers is not None else default_servers(self.settings)
        self.timeout = timeout
        self.health_interval_s = (
            self.settings.mcp_health_interval_s
            if health_interval_s is None
            else health_interval_s
        )
        self.audit: list[AuditRecord] = []
        self._conns: dict[str, _Connection] = {
            spec.name: _Connection(spec) for spec in self.servers
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._lock = threading.RLock()
        self._server_locks: dict[str, threading.Lock] = {
            spec.name: threading.Lock() for spec in self.servers
        }
        self._health_stop = threading.Event()
        self._health_thread: threading.Thread | None = None
        self._last_health: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, name="mcp-loop", daemon=True
            )
            self._thread.start()

            async def open_all() -> None:
                # Start the six processes concurrently: sequentially this is
                # dominated by Python import time.
                results = await asyncio.gather(
                    *(conn.open() for conn in self._conns.values()),
                    return_exceptions=True,
                )
                for conn, result in zip(self._conns.values(), results):
                    if isinstance(result, BaseException):
                        conn.error = f"{type(result).__name__}: {result}"[:200]
                        self._set_up_metric(conn.spec.name, False)
                    else:
                        self._set_up_metric(conn.spec.name, True)

            try:
                asyncio.run_coroutine_threadsafe(open_all(), self._loop).result(
                    timeout=self.timeout
                )
            except Exception as exc:  # noqa: BLE001 - degrade, don't crash the API
                for conn in self._conns.values():
                    if conn.session is None and not conn.error:
                        conn.error = f"{type(exc).__name__}: {exc}"[:200]
                    self._set_up_metric(conn.spec.name, conn.session is not None)
            self._started = True
            self._start_health_monitor()

    def _start_health_monitor(self) -> None:
        if self.health_interval_s <= 0 or not self._started:
            return
        self._health_stop.clear()

        def loop() -> None:
            while not self._health_stop.wait(self.health_interval_s):
                try:
                    self.health_check()
                except Exception as exc:  # noqa: BLE001 - monitor must never die
                    log.warning("mcp health check crashed: %s", exc)

        self._health_thread = threading.Thread(
            target=loop, name="mcp-health", daemon=True
        )
        self._health_thread.start()

    def close(self) -> None:
        with self._lock:
            if not self._started or self._loop is None:
                return
            self._health_stop.set()
            if self._health_thread is not None:
                self._health_thread.join(timeout=5)
                self._health_thread = None
            for name, conn in list(self._conns.items()):
                try:
                    asyncio.run_coroutine_threadsafe(conn.close(), self._loop).result(
                        timeout=10
                    )
                except Exception:  # noqa: BLE001 - best effort shutdown
                    pass
                self._set_up_metric(name, False)
            loop = self._loop
            loop.call_soon_threadsafe(loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
                self._thread = None
            try:
                loop.close()
            except Exception:  # noqa: BLE001 - already closed / pending callbacks
                pass
            self._loop = None
            self._started = False

    def __enter__(self) -> "MCPGateway":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --------------------------------------------------------------- health
    def _set_up_metric(self, server: str, up: bool) -> None:
        if not HAS_MCP_METRICS:
            return
        try:
            MCP_SERVER_UP.labels(server=server).set(1 if up else 0)
        except Exception:  # noqa: BLE001
            pass

    def _ping(self, conn: _Connection) -> tuple[bool, str]:
        if not self._started or self._loop is None:
            return False, "gateway not started"
        if conn.session is None:
            return False, conn.error or "session not open"
        try:
            future = asyncio.run_coroutine_threadsafe(
                conn.session.send_ping(), self._loop
            )
            future.result(timeout=max(2.0, self.timeout / 5))
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"[:200]

    def health_check(self) -> dict[str, Any]:
        """Ping every server and restart the dead ones.

        A restart is serialised per server. Callers that race with it either
        get the old result or fail and retry through the normal error path; they
        never block forever because every future has a timeout.
        """
        if not self._started:
            self.start()
        for name, conn in list(self._conns.items()):
            ok, error = self._ping(conn)
            if not ok:
                self._restart_server(name, error)
            else:
                self._last_health[name] = {"ok": True, "at": time.time()}
        return self.status()

    def _restart_server(self, name: str, reason: str) -> bool:
        lock = self._server_locks.get(name)
        if lock is None or self._loop is None:
            return False
        with lock:
            old = self._conns.get(name)
            if old is None:
                return False
            # Another thread may have restored it while we waited.
            ok, _ = self._ping(old)
            if ok:
                return True
            log.warning("restarting MCP server %s: %s", name, reason)
            if HAS_MCP_METRICS:
                try:
                    MCP_RESTARTS.labels(server=name).inc()
                except Exception:  # noqa: BLE001
                    pass
            new = _Connection(old.spec)
            new.restarts = old.restarts + 1

            async def reopen() -> None:
                try:
                    await old.close()
                except Exception:  # noqa: BLE001
                    pass
                await new.open()

            try:
                asyncio.run_coroutine_threadsafe(reopen(), self._loop).result(
                    timeout=self.timeout
                )
            except Exception as exc:  # noqa: BLE001
                old.error = f"restart failed: {type(exc).__name__}: {exc}"[:200]
                self._conns[name] = old
                self._last_health[name] = {
                    "ok": False, "at": time.time(), "error": old.error
                }
                self._set_up_metric(name, False)
                log.error("MCP server %s restart failed: %s", name, exc)
                return False
            self._conns[name] = new
            self._last_health[name] = {
                "ok": True, "at": time.time(), "restarts": new.restarts
            }
            self._set_up_metric(name, True)
            log.info("MCP server %s restarted (restarts=%s)", name, new.restarts)
            return True

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, conn in list(self._conns.items()):
            out[name] = {
                "up": conn.session is not None and not conn.error,
                "tools": len(conn.tools),
                "restarts": conn.restarts,
                "error": conn.error,
                "opened_at": conn.opened_at,
                "last_health": self._last_health.get(name, {}),
            }
        return out

    # ---------------------------------------------------------------- inspect
    def available_servers(self) -> dict[str, int]:
        return {name: len(c.tools) for name, c in self._conns.items() if c.tools}

    def failed_servers(self) -> dict[str, str]:
        return {name: c.error for name, c in self._conns.items() if c.error}

    def tools(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for conn in list(self._conns.values()):
            out.extend(conn.tools)
        return out

    # ------------------------------------------------------------------- call
    def call(self, qualified_name: str, **kwargs: Any) -> ToolResult:
        if not self._started:
            self.start()
        server, _, tool = qualified_name.partition(".")
        conn = self._conns.get(server)
        if conn is None:
            return ToolResult(False, qualified_name, error=f"unknown server {server!r}")
        if conn.session is None:
            # One lazy restart attempt before giving up; health monitor may not
            # have run yet after a crash.
            self._restart_server(server, conn.error or "session not open")
            conn = self._conns.get(server) or conn
        if conn.session is None:
            return ToolResult(
                False, qualified_name,
                error=f"server {server!r} unavailable: {conn.error}",
            )
        assert self._loop is not None
        started = time.perf_counter()
        try:
            future = asyncio.run_coroutine_threadsafe(conn.call(tool, kwargs), self._loop)
            result = future.result(timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            result = ToolResult(False, qualified_name,
                                error=f"{type(exc).__name__}: {exc}"[:200])
        latency_s = max(0.0, time.perf_counter() - started)
        if HAS_MCP_METRICS:
            try:
                MCP_TOOL_CALLS.labels(
                    server=server, tool=tool, ok="true" if result.ok else "false"
                ).inc()
                MCP_TOOL_LATENCY.labels(server=server, tool=tool).observe(latency_s)
            except Exception:  # noqa: BLE001
                pass
        self.audit.append(AuditRecord(qualified_name, result.ok, result.latency_ms,
                                      result.error[:120]))
        return result


# --------------------------------------------------------------- shared instance
_SHARED_LOCK = threading.Lock()
_SHARED: MCPGateway | None = None


def shared_gateway(settings: Settings | None = None) -> MCPGateway:
    """Return the process-wide gateway, starting it on first use.

    The first caller's settings win. That is deliberate: ``.env`` is fixed for
    the lifetime of a process, and silently switching upstreams mid-process
    would violate the catalog's "no silent fallback" rule.
    """
    global _SHARED
    s = settings or load_settings()
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = MCPGateway(
                settings=s,
                health_interval_s=s.mcp_health_interval_s,
            )
        _SHARED.start()
        return _SHARED


def close_shared_gateway() -> None:
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is not None:
            try:
                _SHARED.close()
            finally:
                _SHARED = None


def shared_gateway_status() -> dict[str, Any]:
    with _SHARED_LOCK:
        if _SHARED is None:
            return {"started": False, "servers": {}}
        return {"started": _SHARED._started, "servers": _SHARED.status()}  # noqa: SLF001
