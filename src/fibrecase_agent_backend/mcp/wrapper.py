"""A :class:`~..tools.base.Tool` that forwards one remote MCP tool call.

One :class:`McpTool` wraps *one* tool discovered from *one* configured remote
MCP server. It is a **standard** ``Tool`` in every respect the registry and the
phase-3 gate care about:

* ``name`` is the stable, namespaced local name ``mcp_<server>__<remote>`` —
  never the bare remote name, so two servers exposing a same-named tool coexist
  without colliding (and neither can shadow a built-in);
* ``default_permission`` is :attr:`ToolPermission.ASK` **unconditionally** — a
  remote tool is never assumed read-only just because the remote claims it is;
  the owner can still pin any of these to ``allow``/``deny`` via
  ``MCP_PERMISSIONS_FILE`` on the namespaced local name;
* ``parameters`` is the remote tool's ``input_schema`` mapped through verbatim,
  so it is schema-validated by the *existing* registry gate before any network
  request is made.

``execute()`` is the **only** thing it does: forward ``arguments`` to the
connected session's ``call_tool`` and map the response to a bounded,
**non-echoing** string result. It deliberately does **no** authentication,
approval, argument validation, timeout, or audit of its own — those live in the
phase-3 tool loop (``agent/tool_loop.py``), which wraps every registered tool,
MCP or built-in, identically. The wrapper never touches the loop's security
boundaries and never leaks the endpoint, headers, token, the remote exception
body, the tool arguments, the server's instructions, or any non-text content.

This module imports only ``..tools`` (the ``Tool`` interface), the MCP SDK
types, and the stdlib. It knows nothing about Telegram, the database, the OpenAI
SDK, or ``AgentService``.
"""

from __future__ import annotations

import logging
from typing import Any

from ..tools import Tool, ToolPermission

logger = logging.getLogger("mcp")

# The remote ``input_schema`` must map to a JSON-Schema object the registry's
# ``check_schema`` accepts. This is the canonical minimum shape.
_DEFAULT_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}

# A remote tool name may only use tool-name-safe characters, and the full
# namespaced local name must stay within the audit table's ``tool_name``
# column (``String(128)``). ``mcp_`` (4) + server (<=32) + ``__`` (2) leaves at
# most 90 characters for the remote segment.
_MAX_REMOTE_TOOL_NAME_LEN = 90


def local_tool_name(server_name: str, remote_tool_name: str) -> str:
    """The stable namespaced local name for one remote tool.

    ``mcp_<server_name>__<remote_tool_name>``. The ``mcp_`` prefix and the
    ``__`` separator keep the two segments unambiguous and — because the server
    and remote segments are both drawn from ``[A-Za-z0-9_-]`` (see the config
    and :func:`is_valid_remote_tool_name`) — the local name is itself a valid
    ``[A-Za-z0-9_-]+`` tool name the registry/policy/audit all accept.
    """
    return f"mcp_{server_name}__{remote_tool_name}"


def is_valid_remote_tool_name(remote_tool_name: str, *, server_name: str) -> bool:
    """Whether a remote tool name maps to a registrable, length-safe local name.

    ``True`` only when the remote name is non-empty, matches ``[A-Za-z0-9_-]+``
    (so it cannot inject the ``mcp_``/``__`` delimiters or any other character),
    and the resulting :func:`local_tool_name` fits the 128-char ``tool_name``
    column. An invalid name makes the *whole server* fail atomic discovery
    (see :mod:`.manager`) rather than register a broken or colliding tool.
    """
    if not remote_tool_name:
        return False
    if any(not (c.isalnum() or c in "_-") for c in remote_tool_name):
        return False
    if len(remote_tool_name) > _MAX_REMOTE_TOOL_NAME_LEN:
        return False
    return len(local_tool_name(server_name, remote_tool_name)) <= 128


class McpTool(Tool):
    """A standard :class:`Tool` backed by one remote MCP tool.

    ``session`` is the connected :class:`mcp.ClientSession` for the tool's
    server. The wrapper holds it only to issue ``call_tool``; everything else
    (identity, permission, schema, approval summary) is plain ``Tool`` surface
    the registry and the gate consume exactly as for a built-in.
    """

    default_permission = ToolPermission.ASK

    def __init__(
        self,
        *,
        server_name: str,
        remote_name: str,
        description: str,
        parameters: dict[str, Any],
        session: Any,
        max_result_chars: int,
    ) -> None:
        self._remote_name = remote_name
        self._session = session
        self._max_result_chars = max_result_chars
        self.name = local_tool_name(server_name, remote_name)
        # A short, remote-invariant marker so the model (and the approval card)
        # know this is a configured remote tool; the remote description follows.
        # The server's *instructions* are deliberately never part of this.
        self.description = f"(🌐Remote) {(description or '')}".strip()
        self.parameters = parameters if isinstance(parameters, dict) and parameters else dict(_DEFAULT_PARAMETERS)

    def approval_summary(self, arguments: dict[str, Any]) -> str:
        # Show the tool's *purpose* (self.description: a fixed remote-invariant
        # marker plus the server-declared description) — this is the "what it
        # does" line. The (remote) arguments are shown separately on the card by
        # the approval provider (as a readable-JSON "Arguments:" block), not here.
        return self.description

    async def execute(self, arguments: dict[str, Any]) -> str:
        """Forward one call to the remote server and return a bounded result.

        Returns a *stable, non-echoing* string. Text content blocks are joined
        with newlines in order; any other outcome (error flag, non-text block,
        empty result, oversized total, transport/protocol failure) maps to a
        fixed code string that carries no remote detail. It must **never**
        raise a raw remote exception — the phase-3 gate also has a
        ``tool_execution_failed`` path, but the wrapper prefers a more specific,
        still-safe code.
        """
        try:
            result = await self._session.call_tool(self._remote_name, arguments or {})
        except Exception as exc:  # transport / protocol / session error
            # Log only the exception *class* and the tool name — never the
            # exception body (it can embed the endpoint or a token).
            logger.warning(
                "mcp call failed",
                extra={"tool": self.name, "code": "mcp_unavailable", "exception": type(exc).__name__},
            )
            return _result("mcp_unavailable", "The remote MCP tool could not be reached.")

        # ``call_tool`` returns a ``CallToolResult`` in the normal case; an
        # input-required or bare ``Result`` does not carry ``is_error`` and is
        # out of scope for this phase — report it, don't guess.
        if _field(result, "is_error", default=_SENTINEL) is _SENTINEL:
            return _result("mcp_unsupported_result", "The remote MCP tool returned an unsupported result type.")

        if _truthy(_field(result, "is_error")):
            return _result("mcp_tool_error", "The remote MCP tool reported an error.")

        content = _field(result, "content", None) or []
        texts: list[str] = []
        total = 0
        for block in content:
            if _field(block, "type") != "text":
                # An image / audio / resource / tool-use / structured block: we
                # only accept bounded text in this phase.
                return _result("mcp_unsupported_result", "The remote MCP tool returned a non-text result.")
            text = _field(block, "text")
            if not isinstance(text, str) or text == "":
                return _result("mcp_unsupported_result", "The remote MCP tool returned a non-text result.")
            texts.append(text)
            total += len(text)
        if not texts:
            return _result("mcp_unsupported_result", "The remote MCP tool returned no text.")
        if total > self._max_result_chars:
            # Do **not** truncate or echo the leading bytes — the operator set
            # a hard bound on how much remote text will reach the model.
            return _result("mcp_result_too_large", "The remote MCP tool result was too large.")
        return "\n".join(texts)


# ---------------------------------------------------------------------------
# result-shape helpers (defensive — the remote is untrusted)
# ---------------------------------------------------------------------------
_SENTINEL = object()


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from ``obj`` whether it is a pydantic model or a dict.

    The real MCP SDK returns pydantic models (``CallToolResult`` /
    ``TextContent`` …) whose fields are snake_case; tests exercise the wrapper
    with plain dicts. This single accessor keeps :meth:`McpTool.execute`
    working against either without the wrapper importing SDK types. A missing
    field returns ``default`` (or :data:`_SENTINEL` when that is the sentinel,
    which the caller uses to *detect* an absent ``is_error``).
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    if hasattr(obj, name):
        value = getattr(obj, name)
        return default if value is None else value
    return default


def _truthy(value: Any) -> bool:
    return bool(value)


def _result(code: str, message: str) -> str:
    # A stable, model-readable one-line result. It never echoes arguments, the
    # endpoint, headers, the token, the remote exception text, or server
    # instructions — only a fixed code and a fixed sentence.
    return f"[{code}] {message}"
