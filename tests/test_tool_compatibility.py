"""The worker-thread wrapper must preserve every existing public MCP contract.

Register modules directly rather than importing server.py, and never execute
tool handlers or initialize a Garmin client. Socket guards enforce offline use.
"""
import asyncio
import importlib
from pathlib import Path
import pkgutil
import socket
import sys

from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import modules
from tool_runtime import GarminMCP


def test_all_module_tool_contracts_match_original_fastmcp(monkeypatch):
    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("Tool registration must not perform network I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)

    original = FastMCP("original-contract-test")
    threaded = GarminMCP("threaded-contract-test")
    registered_modules = []
    for item in sorted(pkgutil.iter_modules(modules.__path__), key=lambda item: item.name):
        module = importlib.import_module(f"modules.{item.name}")
        register = getattr(module, "register_tools", None)
        if register is not None:
            original = register(original)
            threaded = register(threaded)
            registered_modules.append(item.name)

    # Prevent a broken discovery path from passing by comparing two empty apps.
    assert len(registered_modules) == 11
    original_tools = asyncio.run(original.get_tools())
    threaded_tools = asyncio.run(threaded.get_tools())
    assert len(original_tools) == 90
    assert set(threaded_tools) == set(original_tools)

    for name, before in original_tools.items():
        after = threaded_tools[name]
        assert after.name == before.name, name
        assert after.parameters == before.parameters, f"{name}: input schema changed"
        assert after.output_schema == before.output_schema, f"{name}: output schema changed"
        assert after.description == before.description, f"{name}: description changed"
        assert after.title == before.title, f"{name}: title changed"
        assert after.annotations == before.annotations, f"{name}: annotations changed"
