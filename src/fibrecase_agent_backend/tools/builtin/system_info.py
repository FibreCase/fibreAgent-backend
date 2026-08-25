"""Built-in tool: system information (hostname / platform / Python version).

Deliberately stdlib-only (``socket`` + ``platform``) and read-only — it runs
no subprocesses and touches no files. It only reports facts about the process
the agent is running in.
"""

from __future__ import annotations

import json
import platform
import socket

from ..base import Tool
from ..policy import ToolPermission


class SystemInfoTool(Tool):
    """Report the host name, platform, and Python version of the running process."""

    name = "system_info"
    description = "Get basic information about the system running the agent: hostname, platform, and Python version. Takes no arguments."
    # Read-only stdlib facts; normally ``ALLOW`` like the other safe built-ins.
    # Currently set to ``ask`` (deliberately) to exercise the approval flow —
    # flip back to ``ToolPermission.ALLOW`` to restore approval-free behaviour.
    default_permission = ToolPermission.ASK
    parameters: dict[str, object] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict[str, object]) -> str:
        return json.dumps(
            {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "python_version": platform.python_version(),
            }
        )
