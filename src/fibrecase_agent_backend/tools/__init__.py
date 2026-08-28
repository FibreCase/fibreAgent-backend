"""Tool runtime support: the Tool interface, the registry, and built-ins.

This package is intentionally provider- and channel-agnostic. It knows nothing
about Telegram, the database, or the OpenAI SDK — only that a tool has a
name/description/JSON-schema, a declared permission, and an async ``execute``.
The agent's tool loop (:mod:`..agent.tool_loop`) is what drives these through
the LLM, gated by a policy, schema validation, optional approval, a timeout, and
an append-only audit (phase 3).
"""

from __future__ import annotations

from .approval import (
    ApprovalDecision,
    ApprovalRequest,
    ToolApprovalProvider,
)
from .audit import (
    EVENT_APPROVAL_APPROVED,
    EVENT_APPROVAL_DENIED,
    EVENT_APPROVAL_EXPIRED,
    EVENT_APPROVAL_REQUESTED,
    EVENT_AUDIT_UNAVAILABLE,
    EVENT_COMPLETED,
    EVENT_DENIED,
    EVENT_FAILED,
    EVENT_REQUESTED,
    EVENT_STARTED,
    EVENT_TIMED_OUT,
    EVENT_VALIDATION_FAILED,
    RESULT_APPROVAL_DENIED,
    RESULT_APPROVAL_EXPIRED,
    RESULT_AUDIT_UNAVAILABLE,
    RESULT_INVALID_ARGUMENTS,
    RESULT_OK,
    RESULT_TOOL_DENIED,
    RESULT_TOOL_EXECUTION_FAILED,
    RESULT_TOOL_TIMEOUT,
    RESULT_UNKNOWN_TOOL,
    NoopAuditor,
    ToolAuditEvent,
    ToolAuditor,
    error_result,
)
from .base import Tool
from .builtin import build_default_tools
from .policy import (
    ToolPermission,
    ToolPolicy,
    ToolPolicyError,
    FileBackedToolPolicy,
    build_policy,
    parse_permission,
)
from .registry import ToolNotFoundError, ToolRegistry
from .permissions_file import (
    PermissionsFileError,
    atomic_write,
    load_permissions_file,
    merge_permissions,
    parse_permissions_json,
    reconcile_permissions_file,
    serialize,
)

__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolNotFoundError",
    "build_default_tools",
    # policy
    "ToolPermission",
    "ToolPolicy",
    "ToolPolicyError",
    "FileBackedToolPolicy",
    "build_policy",
    "parse_permission",
    # permissions file
    "PermissionsFileError",
    "parse_permissions_json",
    "load_permissions_file",
    "merge_permissions",
    "serialize",
    "atomic_write",
    "reconcile_permissions_file",
    # approval
    "ApprovalDecision",
    "ApprovalRequest",
    "ToolApprovalProvider",
    # audit
    "ToolAuditEvent",
    "ToolAuditor",
    "NoopAuditor",
    "error_result",
    "RESULT_OK",
    "RESULT_UNKNOWN_TOOL",
    "RESULT_TOOL_DENIED",
    "RESULT_INVALID_ARGUMENTS",
    "RESULT_APPROVAL_DENIED",
    "RESULT_APPROVAL_EXPIRED",
    "RESULT_TOOL_TIMEOUT",
    "RESULT_TOOL_EXECUTION_FAILED",
    "RESULT_AUDIT_UNAVAILABLE",
    "EVENT_REQUESTED",
    "EVENT_DENIED",
    "EVENT_VALIDATION_FAILED",
    "EVENT_APPROVAL_REQUESTED",
    "EVENT_APPROVAL_APPROVED",
    "EVENT_APPROVAL_DENIED",
    "EVENT_APPROVAL_EXPIRED",
    "EVENT_STARTED",
    "EVENT_COMPLETED",
    "EVENT_TIMED_OUT",
    "EVENT_FAILED",
    "EVENT_AUDIT_UNAVAILABLE",
]
