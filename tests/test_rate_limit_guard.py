"""Credential-free regression tests for the Garmin request guard."""
import importlib
import sys
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# The client wrapper is tested independently of network and optional libraries.
stub = types.ModuleType("garminconnect")
stub.Garmin = Mock()
stub.GarminConnectAuthenticationError = type("GarminConnectAuthenticationError", (Exception,), {})
with patch.dict(sys.modules, {"garminconnect": stub}):
    gc = importlib.import_module("garmin_client")


class HttpError(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f"{status} Client Error for https://example.invalid/oauth/exchange")
        self.response = types.SimpleNamespace(
            status_code=status,
            headers={} if retry_after is None else {"Retry-After": retry_after},
        )


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.clock = 1000.0
        self.monotonic = patch.object(gc.time, "monotonic", side_effect=lambda: self.clock)
        self.monotonic.start()
        self.addCleanup(self.monotonic.stop)
        self.environ = patch.dict(gc.os.environ, {"GARMIN_RATE_LIMIT_COOLDOWN_SEC": "1800"})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def test_oauth_url_does_not_turn_429_into_auth_error(self):
        self.assertFalse(gc._is_auth_error(HttpError(429)))
        self.assertFalse(gc._is_auth_error(HttpError(500)))
        self.assertTrue(gc._is_auth_error(HttpError(401)))

    def test_initial_login_429_blocks_even_forced_login(self):
        proxy = gc.GarminProxy()
        with patch.object(gc, "_authenticate", side_effect=HttpError(429)) as login:
            with self.assertRaises(gc.GarminRateLimitError):
                proxy.get_activities()
            self.clock += 21
            with self.assertRaises(gc.GarminRateLimitError):
                proxy._ensure(force=True)
            self.assertEqual(login.call_count, 1)
            self.assertEqual(proxy.status()["retry_after_seconds"], 1779)

    def test_cached_method_cannot_bypass_cooldown(self):
        client = types.SimpleNamespace(get_activities=Mock(side_effect=HttpError(429)))
        proxy = gc.GarminProxy()
        with patch.object(gc, "_authenticate", return_value=client) as login:
            retained_method = proxy.get_activities
            with self.assertRaises(gc.GarminRateLimitError):
                retained_method()
            with self.assertRaises(gc.GarminRateLimitError):
                retained_method()
            self.assertEqual(login.call_count, 1)
            self.assertEqual(client.get_activities.call_count, 1)

    def test_preserve_session_and_retry_after(self):
        client = types.SimpleNamespace(get_activities=Mock(side_effect=[HttpError(429, "10000"), ["ok"]]))
        proxy = gc.GarminProxy()
        with patch.object(gc, "_authenticate", return_value=client) as login:
            with self.assertRaises(gc.GarminRateLimitError) as caught:
                proxy.get_activities()
            self.assertEqual(caught.exception.retry_after_seconds, 10000)
            self.clock += 10000
            self.assertEqual(proxy.get_activities(), ["ok"])
            self.assertEqual(login.call_count, 1)
            self.assertEqual(proxy._rate_limit_count, 0)

    def test_wrapped_error_and_http_date_retry_after(self):
        deadline = format_datetime(datetime.fromtimestamp(20000, timezone.utc), usegmt=True)
        outer = RuntimeError("wrapped failure")
        outer.__cause__ = HttpError(429, deadline)
        with patch.object(gc.time, "time", return_value=10000):
            self.assertTrue(gc._is_rate_limit(outer))
            self.assertEqual(gc._retry_after(outer), 10000)

    def test_repeated_429_increases_local_backoff(self):
        proxy = gc.GarminProxy()
        with patch.object(gc, "_authenticate", side_effect=HttpError(429)) as login:
            for delay in (1800, 3600, 7200, 7200):
                with self.assertRaises(gc.GarminRateLimitError) as caught:
                    proxy._ensure()
                self.assertEqual(caught.exception.retry_after_seconds, delay)
                self.clock += delay
            self.assertEqual(login.call_count, 4)

    def test_rate_limit_after_one_auth_refresh_is_guarded(self):
        old = types.SimpleNamespace(get_activities=Mock(side_effect=HttpError(401)))
        new = types.SimpleNamespace(get_activities=Mock(side_effect=HttpError(429)))
        proxy = gc.GarminProxy()
        with patch.object(gc, "_authenticate", side_effect=[old, new]) as login:
            with self.assertRaises(gc.GarminRateLimitError):
                proxy.get_activities()
            with self.assertRaises(gc.GarminRateLimitError):
                proxy.get_activities()
            self.assertEqual(login.call_count, 2)
            self.assertEqual(new.get_activities.call_count, 1)

    def test_retained_method_uses_refreshed_client(self):
        old = types.SimpleNamespace(get_activities=Mock(side_effect=HttpError(401)))
        new = types.SimpleNamespace(get_activities=Mock(return_value=["ok"]))
        proxy = gc.GarminProxy()
        with patch.object(gc, "_authenticate", side_effect=[old, new]) as login:
            method = proxy.get_activities
            self.assertEqual(method(), ["ok"])
            self.assertEqual(method(), ["ok"])
            self.assertEqual(login.call_count, 2)
            self.assertEqual(old.get_activities.call_count, 1)

    def test_non_auth_failure_does_not_login_again(self):
        client = types.SimpleNamespace(get_activities=Mock(side_effect=HttpError(500)))
        proxy = gc.GarminProxy()
        with patch.object(gc, "_authenticate", return_value=client) as login:
            with self.assertRaises(HttpError):
                proxy.get_activities()
            self.assertEqual(login.call_count, 1)

    def test_shared_client_serializes_requests(self):
        state = {"active": 0, "peak": 0}
        lock = threading.Lock()
        def operation():
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.01)
            with lock:
                state["active"] -= 1
            return "ok"
        client = types.SimpleNamespace(get_activities=operation)
        proxy = gc.GarminProxy()
        with patch.object(gc, "_authenticate", return_value=client) as login:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: proxy.get_activities(), range(4)))
            self.assertEqual(results, ["ok"] * 4)
            self.assertEqual(state["peak"], 1)
            self.assertEqual(login.call_count, 1)

    def test_invalid_retry_after_uses_fallback(self):
        for value in ("invalid", "NaN", "-5", "Infinity"):
            self.assertIsNone(gc._retry_after(HttpError(429, value)))

    def test_status_never_authenticates(self):
        with patch.object(gc, "_authenticate") as login:
            self.assertFalse(gc.GarminProxy().status()["authenticated"])
            login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
