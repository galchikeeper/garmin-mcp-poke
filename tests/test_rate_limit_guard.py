"""Offline tests using the real pinned Garmin dependency and fake transports."""
import base64
import json
import logging
import importlib.util
import io
import os
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import garmin_client as gc
from state_store import StateStore, StorageError, atomic_json, read_json


def token(exp=9999999999):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"e30.{payload}.fake"


def tokens(exp=9999999999, refresh="FAKE_REFRESH"):
    return {"di_token": token(exp), "di_refresh_token": refresh, "di_client_id": "FAKE_CLIENT"}


def response(status=200, data=None, retry_after=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data if data is not None else {}).encode()
    if retry_after is not None:
        result.headers["Retry-After"] = str(retry_after)
    return result


class HttpError(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f"{status} Client Error for https://example.invalid/oauth/exchange")
        self.response = response(status, retry_after=retry_after)


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        previous_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_logging)
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "private"
        self.environment = patch.dict(os.environ, {"GARMIN_STATE_DIR": str(self.directory), "GARMIN_RATE_LIMIT_COOLDOWN_SEC": "1800"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.now = 10000.0
        clock = patch.object(gc.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.store = StateStore()
        atomic_json(self.store.tokens, tokens())
        # A missing fake transport must fail the test, never reach Garmin.
        blocked = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network"))
        blocked.start()
        self.addCleanup(blocked.stop)
        blocked_curl = patch("garminconnect.client.cffi_requests.post", side_effect=AssertionError("Unexpected curl network"))
        blocked_curl.start()
        self.addCleanup(blocked_curl.stop)

    def fake_client(self, side_effect=None, result=None):
        return types.SimpleNamespace(get_activities=Mock(side_effect=side_effect, return_value=result or ["ok"]))

    def test_oauth_url_does_not_turn_429_or_500_into_auth(self):
        self.assertFalse(gc._is_auth_error(HttpError(429)))
        self.assertFalse(gc._is_auth_error(HttpError(500)))
        self.assertTrue(gc._is_auth_error(HttpError(401)))

    def test_restore_429_is_saved_and_blocks_new_process(self):
        with patch.object(gc, "restore_client", side_effect=HttpError(429)) as restore:
            with self.assertRaises(gc.GarminRateLimitError):
                gc.GarminProxy().get_activities()
            self.now += 21
            other = gc.GarminProxy()
            with self.assertRaises(gc.GarminRateLimitError):
                other.get_activities()
            self.assertEqual(restore.call_count, 1)
            self.assertEqual(other.status()["retry_after_seconds"], 1779)

    def test_retained_method_checks_shared_cooldown(self):
        client = self.fake_client(side_effect=HttpError(429))
        proxy = gc.GarminProxy()
        retained = proxy.get_activities
        with patch.object(gc, "restore_client", return_value=client):
            for _ in range(2):
                with self.assertRaises(gc.GarminRateLimitError):
                    retained()
        self.assertEqual(client.get_activities.call_count, 1)

    def test_retry_after_is_preserved_and_session_reused(self):
        client = self.fake_client(side_effect=[HttpError(429, "10000"), ["ok"]])
        proxy = gc.GarminProxy()
        with patch.object(gc, "restore_client", return_value=client) as restore:
            with self.assertRaises(gc.GarminRateLimitError) as caught:
                proxy.get_activities()
            self.assertEqual(caught.exception.retry_after_seconds, 10000)
            self.now += 10000
            self.assertEqual(proxy.get_activities(), ["ok"])
            self.assertEqual(restore.call_count, 1)
        self.assertEqual(self.store.read_guard()["rate_limit_count"], 0)

    def test_http_date_and_wrapped_retry_after(self):
        header = format_datetime(datetime.fromtimestamp(20000, timezone.utc), usegmt=True)
        outer = RuntimeError("outer")
        outer.__cause__ = HttpError(429, header)
        self.assertEqual(gc._retry_after(outer), 10000)
        self.assertTrue(gc._is_rate_limit(outer))

    def test_backoff_survives_restarts(self):
        with patch.object(gc, "restore_client", side_effect=HttpError(429)) as restore:
            for expected in (1800, 3600, 7200, 7200):
                with self.assertRaises(gc.GarminRateLimitError) as caught:
                    gc.GarminProxy().get_activities()
                self.assertEqual(caught.exception.retry_after_seconds, expected)
                self.now += expected
            self.assertEqual(restore.call_count, 4)

    def test_invalid_retry_after_uses_fallback(self):
        for value in ("garbage", "NaN", "-1", "Infinity"):
            self.assertIsNone(gc._retry_after(HttpError(429, value)))

    def test_401_stops_automatic_login_across_restart(self):
        client = self.fake_client(side_effect=HttpError(401))
        with patch.object(gc, "restore_client", return_value=client) as restore:
            for _ in range(2):
                with self.assertRaises(gc.GarminSetupError):
                    gc.GarminProxy().get_activities()
            self.assertEqual(restore.call_count, 1)
        self.assertTrue(self.store.read_guard()["auth_required"])

    def test_replacing_tokens_clears_auth_failure_but_not_429(self):
        client = self.fake_client(side_effect=[HttpError(401), ["ok"], HttpError(429)])
        with patch.object(gc, "restore_client", return_value=client):
            with self.assertRaises(gc.GarminSetupError):
                gc.GarminProxy().get_activities()
            atomic_json(self.store.tokens, tokens(refresh="FAKE_REPLACED"))
            self.assertEqual(gc.GarminProxy().get_activities(), ["ok"])
            with self.assertRaises(gc.GarminRateLimitError):
                gc.GarminProxy().get_activities()
            atomic_json(self.store.tokens, tokens(refresh="FAKE_AGAIN"))
            with self.assertRaises(gc.GarminRateLimitError):
                gc.GarminProxy().get_activities()
        self.assertEqual(client.get_activities.call_count, 3)

    def test_500_does_not_trigger_login_refresh(self):
        client = self.fake_client(side_effect=HttpError(500))
        with patch.object(gc, "restore_client", return_value=client) as restore:
            with self.assertRaises(HttpError):
                gc.GarminProxy().get_activities()
            self.assertEqual(restore.call_count, 1)
        self.assertFalse(self.store.read_guard()["auth_required"])

    def test_separate_proxies_serialize_calls(self):
        active = 0
        peak = 0
        mutex = threading.Lock()
        def operation():
            nonlocal active, peak
            with mutex:
                active += 1
                peak = max(active, peak)
            time.sleep(0.01)
            with mutex:
                active -= 1
            return "ok"
        client = types.SimpleNamespace(get_activities=operation)
        proxies = [gc.GarminProxy(), gc.GarminProxy()]
        with patch.object(gc, "restore_client", return_value=client):
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(lambda i: proxies[i % 2].get_activities(), range(8))), ["ok"] * 8)
        self.assertEqual(peak, 1)

    def test_health_and_attribute_lookup_do_not_authenticate(self):
        with patch.object(gc, "restore_client") as restore:
            proxy = gc.GarminProxy()
            self.assertFalse(proxy.status()["authenticated"])
            proxy.get_activities
            restore.assert_not_called()

    def test_corrupt_guard_fails_closed(self):
        self.store.guard.write_text("broken")
        with patch.object(gc, "restore_client") as restore:
            with self.assertRaises(StorageError):
                gc.GarminProxy().get_activities()
            restore.assert_not_called()

    def test_state_write_failure_stops_subsequent_requests(self):
        proxy = gc.GarminProxy()
        with patch.object(proxy._store, "save_guard", side_effect=StorageError("disk full")):
            with self.assertRaises(StorageError):
                proxy.get_activities()
        with patch.object(gc, "restore_client") as restore:
            with self.assertRaises(StorageError):
                proxy.get_activities()
            restore.assert_not_called()

    def test_tokens_and_guard_have_private_permissions(self):
        with patch.object(gc, "restore_client", return_value=self.fake_client()):
            gc.GarminProxy().get_activities()
        for path in (self.store.tokens, self.store.guard, self.store.lock_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)

    def test_symlink_state_file_is_rejected(self):
        target = self.directory / "outside.json"
        atomic_json(target, {})
        self.store.guard.symlink_to(target)
        with self.assertRaises(StorageError):
            gc.GarminProxy().get_activities()

    def test_seed_never_overwrites_rotated_tokens(self):
        with patch.dict(os.environ, {"GARMIN_TOKENS_JSON": json.dumps(tokens(refresh="STALE"))}):
            with patch.object(gc, "restore_client", return_value=self.fake_client()):
                gc.GarminProxy().get_activities()
        self.assertEqual(read_json(self.store.tokens)["di_refresh_token"], "FAKE_REFRESH")

    def test_missing_tokens_imports_modern_seed_once(self):
        self.store.tokens.unlink()
        with patch.dict(os.environ, {"GARMIN_TOKENS_JSON": json.dumps(tokens())}):
            with patch.object(gc, "restore_client", return_value=self.fake_client()):
                gc.GarminProxy().get_activities()
        self.assertEqual(read_json(self.store.tokens), tokens())

    def test_legacy_base64_does_not_attempt_login(self):
        self.store.tokens.unlink()
        with patch.dict(os.environ, {"GARMINTOKENS_BASE64": "FAKE_LEGACY"}):
            with patch.object(gc, "restore_client") as restore:
                with self.assertRaisesRegex(gc.GarminSetupError, "Legacy"):
                    gc.GarminProxy().get_activities()
                restore.assert_not_called()

    def test_render_without_mounted_disk_stops_before_auth(self):
        with patch.dict(os.environ, {"RENDER": "true"}):
            proxy = gc.GarminProxy()
            self.assertFalse(proxy.status()["storage_ready"])
            with self.assertRaises(StorageError):
                proxy.get_activities()

    def real_transport(self, status=200, retry_after=None):
        def send(method, url, **kwargs):
            if "socialProfile" in url:
                return response(data={"displayName": "fake-user", "fullName": "Fake User", "profileId": 1})
            if "user-settings" in url:
                return response(data={"userData": {"measurementSystem": "metric"}})
            return response(status, [{"activityId": 1}], retry_after)
        return send

    def test_real_library_restores_without_any_sso_login(self):
        with patch("requests.sessions.Session.request", side_effect=self.real_transport()) as send:
            with patch.object(gc.Garmin, "login", side_effect=AssertionError("SSO login forbidden")):
                proxy = gc.GarminProxy()
                self.assertEqual(proxy.get_activities(0, 1), [{"activityId": 1}])
                self.assertTrue(proxy.status()["authenticated"])
        self.assertEqual(send.call_count, 3)  # Profile, settings, one read.

    def test_real_api_429_keeps_retry_after_and_has_no_internal_retry(self):
        with patch("requests.sessions.Session.request", side_effect=self.real_transport(429, 10000)) as send:
            with self.assertRaises(gc.GarminRateLimitError) as caught:
                gc.GarminProxy().get_activities(0, 1)
        self.assertEqual(caught.exception.retry_after_seconds, 10000)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(self.store.read_guard()["rate_limit_count"], 1)

    def test_real_profile_429_only_sends_once(self):
        with patch("requests.sessions.Session.request", return_value=response(429)) as send:
            with self.assertRaises(gc.GarminRateLimitError):
                gc.GarminProxy().get_activities(0, 1)
        self.assertEqual(send.call_count, 1)

    def test_real_refresh_429_stops_before_api_and_survives_restart(self):
        atomic_json(self.store.tokens, tokens(exp=1))
        with patch.object(gc.PersistentClient, "_http_post", return_value=response(429, retry_after=5000)) as refresh:
            with patch("requests.sessions.Session.request") as send:
                for _ in range(2):
                    with self.assertRaises(gc.GarminRateLimitError):
                        gc.GarminProxy().get_activities(0, 1)
                send.assert_not_called()
                self.assertEqual(refresh.call_count, 1)
        self.assertEqual(self.store.read_guard()["rate_limit_count"], 1)

    def test_real_refresh_saves_rotated_tokens_and_next_process_reuses(self):
        atomic_json(self.store.tokens, tokens(exp=1))
        refreshed = {"access_token": token(), "refresh_token": "FAKE_ROTATED"}
        with patch.object(gc.PersistentClient, "_http_post", return_value=response(data=refreshed)) as refresh:
            with patch("requests.sessions.Session.request", side_effect=self.real_transport()):
                for _ in range(2):
                    self.assertEqual(gc.GarminProxy().get_activities(0, 1), [{"activityId": 1}])
            self.assertEqual(refresh.call_count, 1)
        self.assertEqual(read_json(self.store.tokens)["di_refresh_token"], "FAKE_ROTATED")

    def test_failed_refresh_persistence_stops_before_api(self):
        atomic_json(self.store.tokens, tokens(exp=1))
        with patch.object(gc.PersistentClient, "_http_post", return_value=response(data={"access_token": token(), "refresh_token": "FAKE_ROTATED"})):
            real_write = gc.atomic_json
            def fail_token_write(path, value):
                if Path(path) == self.store.tokens:
                    raise StorageError("disk full")
                return real_write(path, value)
            with patch.object(gc, "atomic_json", side_effect=fail_token_write):
                with self.assertRaises(StorageError):
                    gc.GarminProxy().get_activities(0, 1)

    def script(self, name):
        path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_local_token_generator_stops_first_429_and_preserves_retry_after(self):
        script = self.script("generate_tokens")
        with patch.object(sys, "argv", ["generate_tokens", "--state-dir", str(self.directory)]):
            with patch("builtins.input", return_value="fake@example.invalid"), patch.object(script.getpass, "getpass", return_value="FAKE_PASSWORD"):
                with patch("requests.sessions.Session.request", return_value=response(429, retry_after=10000)) as network:
                    with patch.object(sys, "stderr", io.StringIO()) as output:
                        self.assertEqual(script.main(), 1)
                        self.assertNotIn("FAKE_PASSWORD", output.getvalue())
                    self.assertEqual(network.call_count, 1)
        self.assertEqual(self.store.read_guard()["blocked_until"], self.now + 10000)
        self.assertEqual(self.store.read_guard()["rate_limit_count"], 1)
        self.assertEqual(read_json(self.store.tokens), tokens())

    def test_token_import_preserves_active_cooldown(self):
        script = self.script("import_tokens")
        self.store.save_guard({"blocked_until": self.now + 1000, "rate_limit_count": 2, "auth_required": True})
        seed = self.directory / "new.json"
        atomic_json(seed, tokens(refresh="FAKE_REPLACEMENT"))
        with patch.object(sys, "argv", ["import_tokens", str(seed)]), patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(script.main(), 0)
        state = self.store.read_guard()
        self.assertFalse(state["auth_required"])
        self.assertEqual(state["blocked_until"], self.now + 1000)
        self.assertEqual(state["rate_limit_count"], 2)
        self.assertEqual(read_json(self.store.tokens)["di_refresh_token"], "FAKE_REPLACEMENT")

    def test_local_generator_writes_new_tokens_without_printing_them(self):
        script = self.script("generate_tokens")
        def network(method, url, **kwargs):
            if "/mobile/api/login" in url:
                return response(data={"responseStatus": {"type": "SUCCESSFUL"}, "serviceTicketId": "FAKE_TICKET"})
            return self.real_transport()(method, url, **kwargs)
        di_response = response(data={"access_token": token(), "refresh_token": "FAKE_NEW_REFRESH"})
        with patch.object(sys, "argv", ["generate_tokens", "--state-dir", str(self.directory)]):
            with patch("builtins.input", return_value="fake@example.invalid"), patch.object(script.getpass, "getpass", return_value="FAKE_PASSWORD"):
                with patch("requests.sessions.Session.request", side_effect=network), patch("garminconnect.client.cffi_requests.post", return_value=di_response):
                    with patch.object(sys, "stdout", io.StringIO()) as output:
                        self.assertEqual(script.main(), 0)
                        self.assertNotIn("FAKE_NEW_REFRESH", output.getvalue())
                        self.assertNotIn("FAKE_PASSWORD", output.getvalue())
        self.assertEqual(read_json(self.store.tokens)["di_refresh_token"], "FAKE_NEW_REFRESH")


if __name__ == "__main__":
    unittest.main()
