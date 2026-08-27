"""Startup discovery + lifecycle for configured MCP servers (Streamable HTTP or stdio).

:class:`McpManager` owns, for the lifetime of the application, one connected
:class:`mcp.ClientSession` per configured server. At :meth:`start` it brings up
each server over its transport (Streamable HTTP **or** a spawned stdio process),
then runs ``initialize`` → ``tools/list``, and wraps every discovered tool as an
:class:`~.wrapper.McpTool`. At :meth:`close` it shuts down every server in one
place (for a stdio server, closing its transport tears down the child process).

Design constraints (from the phase-4 spec):

* **Per-server failure isolation** — one server that fails to connect (a stdio
  command that cannot spawn, or an endpoint that is unreachable), initialise, or
  list its tools is marked *unavailable* (with a stable code) and skipped; every
  other server, and the built-in tools, still come up. The bot must never fail
  to start because an *optional* MCP server is down.
* **Atomic per-server discovery** — a server's tools are all-or-nothing: if any
  one discovered tool's name or ``input_schema`` is invalid (or would collide
  with an already-registered name), *none* of that server's tools are registered
  (the whole server is dropped and marked ``mcp_invalid_tool``).
* **No reconnect** — this phase does not reconnect. A healthy session that later
  drops (or a stdio process that exits) causes a ``call_tool`` to raise, which
  the wrapper maps to ``mcp_unavailable``; a fresh discovery happens only on the
  next process start.

The manager depends only on the MCP SDK, ``httpx2`` (via the SDK's client
helper, http path only), and :mod:`..config`'s :class:`McpServer`. It knows
nothing about Telegram, the database, the OpenAI SDK, or ``AgentService`` — it
merely yields ``Tool`` objects for the composition root to register and a safe
:meth:`status` for the ``/mcp_status`` command.

It logs **only** the server name and a stable code (plus, on an exception, the
exception *class*). It never logs the full URL, the host, a header, the token,
a stdio ``command``/``args``/``env``/``cwd``, a tool's description/schema, the
server's instructions, or any error body.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from jsonschema import Draft202012Validator

from ..config import McpServer
from ..tools import Tool
from .auth.oauth_auth import McpOAuthAuth
from .wrapper import McpTool, is_valid_remote_tool_name

logger = logging.getLogger("mcp")

# Stable, non-echoing codes (surfaced in logs and in the /mcp_status state, but
# never with a URL / header / token / description). Startup failures:
CODE_CONNECT_FAILED = "mcp_connect_failed"
CODE_INITIALIZE_FAILED = "mcp_initialize_failed"
CODE_DISCOVERY_FAILED = "mcp_discovery_failed"
CODE_INVALID_TOOL = "mcp_invalid_tool"
# Phase 4.x: an OAuth-configured server whose OAuth infrastructure was not wired.
CODE_OAUTH_NOT_CONFIGURED = "mcp_oauth_not_configured"


@dataclass
class _ServerState:
    """Per-server discovery + runtime state (the status subset is what leaks out)."""

    name: str
    spec: McpServer
    available: bool = False
    code: str | None = None  # a stable code when not available; None when healthy
    tool_count: int = 0
    tools: list[Tool] = field(default_factory=list)
    # Runtime handles kept for the app lifetime; only used to drive call_tool
    # (via the wrappers) and to close on shutdown. Never part of the status.
    session: Any | None = None


class McpManager:
    """Connects, discovers, holds, and closes remote MCP server sessions.

    Construct it only when tools are enabled **and** at least one server is
    configured — with no servers there is nothing to connect and the manager
    must not exist (the composition root guards this).
    """

    def __init__(
        self,
        servers: "tuple[McpServer, ...] | list[McpServer]",
        *,
        connect_timeout_seconds: float,
        max_result_chars: int,
        oauth_auth_factory: "Callable[[McpServer], httpx2.Auth] | None" = None,
    ) -> None:
        self._servers = list(servers)
        self._connect_timeout = connect_timeout_seconds
        self._max_result_chars = max_result_chars
        # Phase 4.x: for a server whose ``auth_type == "oauth"``, this factory
        # returns the per-user :class:`McpOAuthAuth` that attaches the
        # requesting user's access token to each request. ``None`` = no OAuth
        # infrastructure was wired (an OAuth server then fails to start with a
        # stable code instead of connecting unauthenticated).
        self._oauth_auth_factory = oauth_auth_factory
        self._states: list[_ServerState] = []
        # One AsyncExitStack per healthy server keeps its http client, the
        # Streamable HTTP transport, and the ClientSession entered for the app
        # lifetime; closing the stack tears that server down in order.
        self._stacks: list[contextlib.AsyncExitStack] = []
        self._started = False

    # ------------------------------------------------------------------ start
    async def start(self, existing_names: "set[str] | frozenset[str] | list[str] | None" = None) -> None:
        """Connect + initialise + discover every configured server.

        ``existing_names`` are the tool names already registered (the built-ins,
        passed by the composition root before any MCP tool is added). A server
        whose discovered tools collide with any of them — or with another tool
        the *same* server lists twice — is rejected atomically (see
        :meth:`_build_tools`).

        Never raises: a failure on one server marks it unavailable and moves on
        to the next, so a bad endpoint can never prevent the bot from starting.
        """
        existing = set(existing_names) if existing_names else set()
        # As we register servers in order, their names are added to this set so a
        # later server can't collide with an earlier one (impossible by the
        # namespaced form, but enforced anyway for defence in depth).
        taken = set(existing)
        for spec in self._servers:
            state = _ServerState(name=spec.name, spec=spec)
            await self._start_one(state, taken)
            self._states.append(state)
            if state.available:
                for tool in state.tools:
                    taken.add(tool.name)
                logger.info(
                    "mcp server ready",
                    extra={"server": spec.name, "code": None, "tools": state.tool_count},
                )
            else:
                logger.warning(
                    "mcp server unavailable",
                    extra={"server": spec.name, "code": state.code},
                )
        self._started = True

    async def _start_one(self, state: _ServerState, taken: set[str]) -> None:
        """Bring up one server; on any failure leave it marked unavailable."""
        spec = state.spec
        # Phase 4.x: an OAuth server requires the OAuth infrastructure (the
        # per-user token auth) to have been wired by the composition root.
        # Without it we must **not** connect unauthenticated — fail with a
        # stable code; the rest of the fleet still starts.
        if spec.auth_type == "oauth" and self._oauth_auth_factory is None:
            logger.warning(
                "mcp server unavailable (oauth not configured)",
                extra={"server": spec.name, "code": CODE_OAUTH_NOT_CONFIGURED},
            )
            state.code = CODE_OAUTH_NOT_CONFIGURED
            state.available = False
            return
        stack = contextlib.AsyncExitStack()
        try:
            # The only place the two transports diverge: http builds an outbound
            # client (bearer header / OAuth) and a Streamable HTTP transport;
            # stdio spawns the operator-configured process. Both yield the same
            # (read_stream, write_stream) that ClientSession consumes below.
            if spec.transport == "stdio":
                streams = await stack.enter_async_context(
                    stdio_client(
                        StdioServerParameters(
                            command=spec.command,
                            args=list(spec.args),
                            env=dict(spec.env) or None,
                            cwd=spec.cwd or None,
                        )
                    )
                )
            else:
                http_client = self._build_http_client(spec)
                await stack.enter_async_context(http_client)
                # Streamable HTTP transport (connects lazily; the real round-trip
                # is driven by initialize() below, which we bound with wait_for).
                streams = await stack.enter_async_context(
                    streamable_http_client(spec.url, http_client=http_client, terminate_on_close=True)
                )
            read_stream, write_stream = streams
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream, read_timeout_seconds=self._connect_timeout)
            )
            # The initialisation round-trip is where DNS/TLS/auth/handshake
            # surface. A timeout is a connect-level failure; anything else that
            # fails here is an initialise failure (bad protocol, rejected auth,
            # malformed server response).
            try:
                await asyncio.wait_for(session.initialize(), timeout=self._connect_timeout)
            except asyncio.TimeoutError:
                raise _StartFailure(CODE_CONNECT_FAILED) from None
            except Exception as exc:  # MCPError / httpx2.HTTPError / anything else
                logger.debug("mcp initialise failed", extra={"exception": type(exc).__name__})
                raise _StartFailure(CODE_INITIALIZE_FAILED) from None

            # tools/list discovery.
            try:
                list_result = await asyncio.wait_for(session.list_tools(), timeout=self._connect_timeout)
            except asyncio.TimeoutError:
                raise _StartFailure(CODE_DISCOVERY_FAILED) from None
            except Exception as exc:
                logger.debug("mcp discovery failed", extra={"exception": type(exc).__name__})
                raise _StartFailure(CODE_DISCOVERY_FAILED) from None

            tools = self._build_tools(spec, list_result, session, taken)
            if tools is None:
                # Atomic discovery failure: drop the whole server (clean up its
                # transport) and mark it unavailable.
                raise _StartFailure(CODE_INVALID_TOOL) from None

            state.session = session
            state.tools = tools
            state.tool_count = len(tools)
            state.available = True
            self._stacks.append(stack)  # keep the transport/session alive
        except _StartFailure as failure:
            state.code = failure.code
            state.available = False
        except Exception as exc:  # defensive: a truly unexpected error
            state.code = CODE_CONNECT_FAILED
            state.available = False
            logger.warning(
                "mcp server failed to start",
                extra={"server": spec.name, "code": state.code, "exception": type(exc).__name__},
            )
        finally:
            # On any failure the per-server stack must be unwound now (it holds
            # a live http client / transport). On success it is already kept in
            # self._stacks, so we must **not** close it here.
            if not state.available:
                with contextlib.suppress(Exception):
                    await stack.aclose()

    def _build_tools(
        self,
        spec: McpServer,
        list_result: Any,
        session: Any,
        taken: set[str],
    ) -> "list[Tool] | None":
        """Map one server's discovered tools to ``McpTool``s, or ``None`` on an
        invalid schema/name **or a name collision** (atomic rejection of the
        *whole* server).

        A collision is any local name that is already registered (a built-in or
        an earlier server) or repeated within this same server's own list. By the
        ``mcp_<server>__<tool>`` form cross-server and built-in collisions are
        structurally impossible, so this mainly guards against a server that
        lists the same remote tool twice.
        """
        remote_tools = getattr(list_result, "tools", None) or []
        tools: list[Tool] = []
        local_names: set[str] = set()
        for remote in remote_tools:
            remote_name = _attr_or_get(remote, "name")
            if not isinstance(remote_name, str) or not is_valid_remote_tool_name(remote_name, server_name=spec.name):
                logger.debug("mcp invalid remote tool name", extra={"server": spec.name, "code": CODE_INVALID_TOOL})
                return None
            local_name = f"mcp_{spec.name}__{remote_name}"
            if local_name in taken or local_name in local_names:
                # Collides with a built-in / earlier server / its own sibling.
                logger.debug("mcp tool name collision", extra={"server": spec.name, "code": CODE_INVALID_TOOL})
                return None
            description = _attr_or_get(remote, "description")
            description = description if isinstance(description, str) else ""
            parameters = _attr_or_get(remote, "input_schema")
            if not _is_valid_json_schema(parameters):
                # A tool with no usable JSON-Schema is invalid; reject atomically.
                logger.debug("mcp invalid remote tool schema", extra={"server": spec.name, "code": CODE_INVALID_TOOL})
                return None
            local_names.add(local_name)
            tools.append(
                McpTool(
                    server_name=spec.name,
                    remote_name=remote_name,
                    description=description,
                    parameters=parameters,
                    session=session,
                    max_result_chars=self._max_result_chars,
                )
            )
        return tools

    # ----------------------------------------------------------------- status
    def tools(self) -> list[Tool]:
        """All discovered tools, in registration order.

        Order: configured servers in order, each server's tools in the order the
        SDK returned them. The composition root ``add``s these to the registry
        *after* the built-ins, so built-ins stay first. Only *available* servers
        contribute tools.
        """
        result: list[Tool] = []
        for state in self._states:
            if state.available:
                result.extend(state.tools)
        return result

    def status(self) -> list[dict[str, Any]]:
        """A safe, scope-free view for ``/mcp_status``.

        One entry per configured server: ``{"name", "available", "tool_count"}``.
        No URL, host, token, header, description, schema, instructions, or error
        detail is included — the operator can correlate by the server *name* only.
        """
        return [
            {"name": s.name, "available": s.available, "tool_count": s.tool_count}
            for s in self._states
        ]

    @property
    def total_tools(self) -> int:
        """The number of *available* MCP tools (sum over healthy servers)."""
        return sum(s.tool_count for s in self._states if s.available)

    def __len__(self) -> int:
        return len(self._servers)

    # --------------------------------------------------------------- shutdown
    async def close(self) -> None:
        """Shut down every server's transport/session. Idempotent, never raises."""
        if not self._started:
            return
        for stack in self._stacks:
            with contextlib.suppress(Exception):
                await stack.aclose()
        self._stacks.clear()
        for state in self._states:
            state.available = False
            state.session = None
        logger.info("mcp servers closed", extra={"servers": len(self._states)})

    # ----------------------------------------------------------------- helpers
    def _build_http_client(self, spec: McpServer) -> Any:
        """The outbound HTTP client for one server.

        Two mutually-exclusive authentication sources (enforced at config
        parse):

        * ``bearer_token_env`` — an *operator* token: read from the env at call
          time and carried as a fixed ``Authorization`` header (phase 4).
        * ``auth_type == "oauth"`` — a *per-user* token: the ``auth=`` hook
          resolves the **requesting** Telegram user's valid access token for
          this server on every request (phase 4.x) and attaches it; it never
          logs the token, the header, the endpoint, or the user id.

        The token value is never stored on the spec, never logged, never echoed.
        """
        if spec.auth_type == "oauth":
            auth = self._oauth_auth_factory(spec) if self._oauth_auth_factory is not None else None
            return create_mcp_http_client(auth=auth)
        headers: dict[str, str] = {}
        if spec.bearer_token_env:
            token = os.environ.get(spec.bearer_token_env, "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return create_mcp_http_client(headers=headers or None)


class _StartFailure(Exception):
    """Internal: a discovered-but-unhealthy server, carrying its stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _attr_or_get(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a pydantic model or a plain dict (test fakes)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _is_valid_json_schema(parameters: Any) -> bool:
    """Whether a discovered ``input_schema`` is a usable JSON-Schema object.

    Mirrors the registry's own register-time check (:meth:`ToolRegistry.register`
    validates with ``Draft202012Validator.check_schema``). Checking here lets an
    invalid schema reject the *whole server* at discovery (atomic) instead of
    surfacing as a ``ValueError`` mid-``add``. A non-dict is never valid.
    """
    if not isinstance(parameters, dict):
        return False
    try:
        Draft202012Validator.check_schema(parameters)
    except Exception:  # SchemaError and any meta-schema problem
        return False
    return True
