"""Tool registry and built-in tool behaviour.

These are pure unit tests — no LLM, no database, no network. They cover the
required behaviours: registry registration, OpenAI schema generation, and tool
execution (including the unknown-tool and tool-error fallbacks).
"""

from __future__ import annotations

import json
import platform

import pytest

from fibrecase_agent_backend.tools import Tool, ToolNotFoundError, ToolRegistry, build_default_tools
from fibrecase_agent_backend.tools.builtin import (
    EchoTool,
    GetCurrentTimeTool,
    SystemInfoTool,
)


# ---------------------------------------------------------------------------
# required #1 — registry registration
# ---------------------------------------------------------------------------
def test_registry_register_and_lookup():
    reg = ToolRegistry()
    assert reg.names() == []
    assert len(reg) == 0

    reg.register(EchoTool())
    reg.add(GetCurrentTimeTool(), SystemInfoTool())

    assert reg.names() == ["echo", "get_current_time", "system_info"]
    assert len(reg) == 3
    assert "echo" in reg
    assert "get_current_time" in reg
    assert "missing" not in reg
    assert reg.get("echo").name == "echo"


def test_registry_get_unknown_raises():
    reg = ToolRegistry().register(EchoTool())
    with pytest.raises(ToolNotFoundError):
        reg.get("nope")


def test_registry_register_rejects_duplicate_name():
    reg = ToolRegistry().register(EchoTool())
    with pytest.raises(ValueError):
        reg.register(EchoTool())  # same name, no silent shadowing


def test_default_registry_has_three_safe_tools():
    reg = build_default_tools()
    assert reg.names() == ["get_current_time", "echo", "system_info"]


# ---------------------------------------------------------------------------
# required #2 — OpenAI tools schema generation
# ---------------------------------------------------------------------------
def test_registry_openai_schema_shape():
    reg = build_default_tools()
    schema = reg.to_openai_schema()

    assert len(schema) == 3
    by_name = {entry["function"]["name"]: entry for entry in schema}
    assert set(by_name) == {"get_current_time", "echo", "system_info"}

    for entry in schema:
        assert entry["type"] == "function"
        fn = entry["function"]
        assert {"name", "description", "parameters"} <= set(fn)
        assert fn["parameters"]["type"] == "object"


def test_registry_openai_schema_echo_parameters():
    reg = build_default_tools()
    echo = next(e for e in reg.to_openai_schema() if e["function"]["name"] == "echo")
    params = echo["function"]["parameters"]
    assert params["properties"]["message"]["type"] == "string"
    assert params["required"] == ["message"]
    assert echo["function"]["description"]


def test_registry_openai_schema_no_param_tool():
    reg = build_default_tools()
    t = next(e for e in reg.to_openai_schema() if e["function"]["name"] == "get_current_time")
    assert t["function"]["parameters"]["properties"] == {}


def test_empty_registry_produces_empty_schema():
    # Feeds the tool loop's "no tools -> single completion" branch.
    assert ToolRegistry().to_openai_schema() == []


# ---------------------------------------------------------------------------
# required #3 — tool execution (by name, plus failure fallbacks)
# ---------------------------------------------------------------------------
async def test_registry_execute_runs_a_tool():
    reg = ToolRegistry().register(EchoTool())
    assert await reg.execute("echo", {"message": "hello"}) == "hello"


async def test_registry_execute_unknown_tool_raises():
    # At the registry level an unknown name raises ToolNotFoundError; the tool
    # *loop* is responsible for turning that into a readable error for the model.
    reg = ToolRegistry().register(EchoTool())
    with pytest.raises(ToolNotFoundError):
        await reg.execute("does_not_exist", {})


async def test_registry_execute_tool_exception_returns_error_json():
    class Boom(Tool):
        name = "boom"
        description = "always fails"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, arguments):
            raise ValueError("kaboom")

    reg = ToolRegistry().register(Boom())
    out = await reg.execute("boom", {})
    assert "kaboom" in out
    assert "error" in json.loads(out)


async def test_registry_execute_tolerates_missing_arguments():
    reg = ToolRegistry().register(EchoTool())
    # arguments=None must be handled as an empty mapping.
    out = await reg.execute("echo", None)
    assert out == ""


# ---------------------------------------------------------------------------
# built-in tool sanity
# ---------------------------------------------------------------------------
async def test_get_current_time_returns_datetime_string():
    out = await GetCurrentTimeTool().execute({})
    assert len(out) == 19  # "YYYY-MM-DD HH:MM:SS"
    assert out[4] == "-" and out[10] == " "


async def test_echo_returns_input_verbatim():
    assert await EchoTool().execute({"message": "round trip"}) == "round trip"


async def test_system_info_reports_stdlib_facts():
    out = json.loads(await SystemInfoTool().execute({}))
    assert out["python_version"] == platform.python_version()
    assert isinstance(out["hostname"], str) and out["hostname"]
    assert isinstance(out["platform"], str) and out["platform"]
