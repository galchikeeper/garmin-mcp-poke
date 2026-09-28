"""Real FastMCP client/server checks, all Garmin networking mocked."""
import ast
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from fastmcp import Client, FastMCP
from garminconnect import Garmin
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import garmin_client as gc
from state_store import atomic_json
from tool_errors import LegacyToolErrors
from garmin_mcp import GarminMCP
from modules import activity_management, workouts
from test_rate_limit_guard import tokens, response


class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"GARMIN_STATE_DIR": self.tmp.name}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.proxy = gc.GarminProxy()
        atomic_json(self.proxy._store.tokens, tokens())
        self.mcp = GarminMCP("offline-regression")
        self.mcp.add_middleware(LegacyToolErrors())
        activity_management.configure(self.proxy)
        workouts.configure(self.proxy)
        activity_management.register_tools(self.mcp)
        workouts.register_tools(self.mcp)

    def transport(self, method, url, **kwargs):
        if "socialProfile" in url:
            return response(data={"displayName": "fake-user", "profileId": 1})
        if "user-settings" in url:
            return response(data={"userData": {"measurementSystem": "metric"}})
        if "fbt-adaptive" in url:
            return response(data={"workoutId": "fake-uuid", "workoutName": "test", "workoutSegments": []})
        if "schedule" in url:
            return response(data={"workoutId": 123})
        return response(429, retry_after=1800)

    async def test_429_returns_mcp_error_and_repeat_does_not_contact_garmin(self):
        with patch("requests.sessions.Session.request", side_effect=self.transport) as network:
            async with Client(self.mcp) as client:
                for _ in range(2):
                    result = await client.call_tool("get_activities", {"start": 0, "limit": 1}, raise_on_error=False)
                    self.assertTrue(result.is_error)
                    self.assertIn("429", result.content[0].text)
            self.assertEqual(network.call_count, 3)

    async def test_workout_uuid_and_schedule_use_guarded_native_methods(self):
        with patch("requests.sessions.Session.request", side_effect=self.transport) as network:
            async with Client(self.mcp) as client:
                result = await client.call_tool("get_workout_by_id", {"workout_id": "fake-uuid"}, raise_on_error=False)
                self.assertFalse(result.is_error)
                self.assertIn("test", result.content[0].text)
                result = await client.call_tool("schedule_workout", {"workout_id": 123, "calendar_date": "2026-10-04"}, raise_on_error=False)
                self.assertFalse(result.is_error)
                self.assertEqual(json.loads(result.content[0].text)["status"], "success")
            self.assertEqual(network.call_count, 4)

    async def test_complete_server_lists_tools_without_authentication(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network")) as network:
            server = importlib.import_module("server")
            async with Client(server.mcp) as client:
                tools = await client.list_tools()
                self.assertGreaterEqual(len(tools), 90)
            status = await server.health(None)
            self.assertFalse(json.loads(status.body)["authenticated"])
            network.assert_not_called()

    async def test_slow_garmin_call_does_not_block_health_event_loop(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_read(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError("Server event loop was blocked")
            return []

        fake = types.SimpleNamespace(get_activities=slow_read)
        with patch.object(gc, "restore_client", return_value=fake):
            async with Client(self.mcp) as client:
                task = asyncio.create_task(client.call_tool("get_activities", {"start": 0, "limit": 1}))
                try:
                    started = time.monotonic()
                    while not entered.is_set() and time.monotonic() - started < 1:
                        await asyncio.sleep(0.01)
                    self.assertTrue(entered.is_set())
                    self.assertLess(time.monotonic() - started, 1)
                    self.assertTrue(self.proxy.status()["storage_ready"])
                finally:
                    release.set()
                result = await task
                self.assertFalse(result.is_error)

    def test_all_declared_api_methods_exist_in_pinned_dependency(self):
        for path in (Path(__file__).resolve().parents[1] / "src/modules").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "garmin_client":
                    self.assertTrue(hasattr(Garmin, node.attr), f"{path.name}: {node.attr}")


if __name__ == "__main__":
    unittest.main()
