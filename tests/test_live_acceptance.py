"""Offline acceptance checks: no network, credentials, or real Garmin calls."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_live_once.py"
spec = importlib.util.spec_from_file_location("live_acceptance_command", SCRIPT)
command = importlib.util.module_from_spec(spec)
spec.loader.exec_module(command)
NOW = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)
DATE = "2026-09-28"
PRIVATE = "private-token-or-health-value-must-not-leak"


def health(**updates):
    value = {
        "ok": True, "tokens_loaded": True, "authenticated": False,
        "server_password_login_enabled": False, "token_source": "gist",
        "failure_count": 0, "persistence_error": None,
        "last_error": "LOCAL_TOKEN_SEED_REQUIRED",
        "login_cooldown_until": command.iso(NOW - timedelta(seconds=61)),
        "last_success_at": None, "last_login_at": None,
        "last_refresh_at": None, "last_rate_limit_at": None,
        "unexpected_secret": PRIVATE,
    }
    value.update(updates)
    return value


def success_health(**updates):
    return health(**{
        "authenticated": True, "last_error": None,
        "login_cooldown_until": None,
        "last_success_at": command.iso(NOW + timedelta(seconds=1)),
        **updates,
    })


def stats_result(text=None, is_error=False):
    return SimpleNamespace(isError=is_error, content=[SimpleNamespace(
        type="text", text=text if text is not None else json.dumps({"date": DATE, "metric": PRIVATE}),
    )])


class Scenario:
    def __init__(self, tmp_path, before=None, after=None, result=None):
        self.marker = tmp_path / ".secrets" / "live-acceptance.json"
        self.responses = [health() if before is None else before,
                          success_health() if after is None else after]
        self.result = stats_result() if result is None else result
        self.health_calls = 0
        self.tool_calls = []
        self.tool_error = None
        self.health_error = None

    async def fetch_health(self):
        self.health_calls += 1
        if self.health_error:
            raise self.health_error
        return copy.deepcopy(self.responses[min(self.health_calls - 1, 1)])

    async def call_stats(self, requested_date):
        # A durable restrictive marker MUST already exist before tools/call.
        assert self.marker.is_file()
        assert stat.S_IMODE(self.marker.stat().st_mode) == 0o600
        assert json.loads(self.marker.read_text())["status"] == "attempt_started"
        self.tool_calls.append(requested_date)
        if self.tool_error:
            raise self.tool_error
        return self.result

    def run(self, **kwargs):
        return asyncio.run(command.verify_once(
            requested_date=kwargs.pop("requested_date", DATE), marker_path=self.marker,
            fetch_health=self.fetch_health, call_stats=self.call_stats,
            now=lambda: NOW, **kwargs,
        ))


def test_one_successful_read_only_call_and_sanitized_durable_report(tmp_path):
    scenario = Scenario(tmp_path)
    report = scenario.run()
    assert report["status"] == "passed"
    assert scenario.tool_calls == [DATE]
    assert scenario.health_calls == 2
    assert json.loads(scenario.marker.read_text()) == report
    assert PRIVATE not in json.dumps(report)
    assert PRIVATE not in scenario.marker.read_text()
    assert stat.S_IMODE(scenario.marker.parent.stat().st_mode) == 0o700
    assert scenario.run()["status"] == "already_attempted"
    assert scenario.tool_calls == [DATE]
    assert scenario.health_calls == 2


@pytest.mark.parametrize("seconds_ago", [-60, 0, 59])
def test_waits_until_full_sixty_second_margin_without_marker(tmp_path, seconds_ago):
    scenario = Scenario(tmp_path, before=health(
        login_cooldown_until=command.iso(NOW - timedelta(seconds=seconds_ago))))
    report = scenario.run()
    assert report["status"] == "waiting"
    assert report["reason"] == "cooldown_preserved"
    assert not scenario.marker.exists()
    assert scenario.tool_calls == []
    assert scenario.health_calls == 1


@pytest.mark.parametrize("cooldown", [None, command.iso(NOW - timedelta(seconds=60))])
def test_cleared_or_fully_elapsed_cooldown_is_eligible(tmp_path, cooldown):
    scenario = Scenario(tmp_path, before=health(login_cooldown_until=cooldown))
    assert scenario.run()["status"] == "passed"
    assert scenario.tool_calls == [DATE]


@pytest.mark.parametrize("changes", [
    {"ok": False}, {"tokens_loaded": False}, {"token_source": "environment"},
    {"server_password_login_enabled": True}, {"persistence_error": PRIVATE},
    {"login_cooldown_until": PRIVATE}, {"login_cooldown_until": "2026-09-28T13:00:00"},
    {"failure_count": True}, {"authenticated": None},
])
def test_invalid_or_unready_health_blocks_before_marker(tmp_path, changes):
    scenario = Scenario(tmp_path, before=health(**changes))
    report = scenario.run()
    assert report["status"] == "blocked"
    assert not scenario.marker.exists()
    assert scenario.tool_calls == []
    assert PRIVATE not in json.dumps(report)


def test_missing_required_health_field_blocks(tmp_path):
    before = health()
    del before["persistence_error"]
    scenario = Scenario(tmp_path, before=before)
    assert scenario.run()["status"] == "blocked"
    assert not scenario.marker.exists()


def test_preflight_transport_error_is_sanitized_and_does_not_consume_attempt(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.health_error = RuntimeError(PRIVATE)
    report = scenario.run()
    assert report["status"] == "blocked"
    assert PRIVATE not in json.dumps(report)
    assert scenario.tool_calls == []
    assert not scenario.marker.exists()


@pytest.mark.parametrize("text", [
    "Error retrieving stats: " + PRIVATE,
    "No stats found for " + DATE,
    "{}", "[]", "null", '"' + PRIVATE + '"',
    '{"date":"2026-09-27"}',
    '{"date":"2026-09-28","metric":NaN}',
    '{"date":"2026-09-27","date":"2026-09-28"}',
])
def test_application_errors_and_invalid_json_are_not_false_successes(tmp_path, text):
    scenario = Scenario(tmp_path, result=stats_result(text))
    report = scenario.run()
    assert report["status"] == "attempted_failed"
    assert scenario.tool_calls == [DATE]
    assert scenario.marker.exists()
    assert PRIVATE not in json.dumps(report)
    assert PRIVATE not in scenario.marker.read_text()


def test_mcp_error_is_failure_even_if_text_contains_valid_data(tmp_path):
    scenario = Scenario(tmp_path, result=stats_result(is_error=True))
    assert scenario.run()["status"] == "attempted_failed"


@pytest.mark.parametrize("changes", [
    {"authenticated": False}, {"tokens_loaded": False}, {"token_source": "environment"},
    {"server_password_login_enabled": True}, {"failure_count": 1},
    {"last_error": "GARMIN_RATE_LIMITED"}, {"persistence_error": PRIVATE},
    {"login_cooldown_until": command.iso(NOW + timedelta(hours=6))},
    {"last_success_at": None},
    {"last_success_at": command.iso(NOW - timedelta(minutes=5))},
    {"last_success_at": command.iso(NOW + timedelta(minutes=5))},
    {"last_login_at": command.iso(NOW)},
])
def test_success_requires_fresh_healthy_persisted_state(tmp_path, changes):
    scenario = Scenario(tmp_path, after=success_health(**changes))
    report = scenario.run()
    assert report["status"] == "attempted_failed"
    assert report["reason"] == "postflight_state_not_verified"
    assert len(scenario.tool_calls) == 1
    assert PRIVATE not in json.dumps(report)


def test_success_timestamp_must_advance(tmp_path):
    same = command.iso(NOW)
    scenario = Scenario(tmp_path, before=health(last_success_at=same),
                        after=success_health(last_success_at=same))
    assert scenario.run()["status"] == "attempted_failed"


def test_ambiguous_timeout_does_not_retry_even_when_health_reports_success(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.tool_error = TimeoutError(PRIVATE)
    report = scenario.run()
    assert report["status"] == "attempted_unknown"
    assert report["reason"] == "tool_transport_failure_do_not_retry"
    assert PRIVATE not in json.dumps(report)
    assert scenario.run()["status"] == "already_attempted"
    assert scenario.tool_calls == [DATE]


@pytest.mark.parametrize("contents", ["", "unfinished", '{"status":"attempt_started"}'])
def test_existing_marker_prevents_even_preflight_requests(tmp_path, contents):
    scenario = Scenario(tmp_path)
    scenario.marker.parent.mkdir()
    scenario.marker.write_text(contents)
    assert scenario.run()["status"] == "already_attempted"
    assert scenario.health_calls == 0
    assert scenario.tool_calls == []
    assert scenario.marker.read_text() == contents


def test_broken_symlink_marker_is_not_replaced(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.marker.parent.mkdir()
    scenario.marker.symlink_to(tmp_path / "absent")
    assert scenario.run()["status"] == "already_attempted"
    assert scenario.health_calls == 0
    assert scenario.marker.is_symlink()


def test_marker_creation_race_blocks_call(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path)
    original_open = command.os.open

    def competing_open(path, flags, mode):
        assert flags & os.O_EXCL
        assert flags & os.O_CREAT
        assert mode == 0o600
        scenario.marker.write_text("other attempt")
        return original_open(path, flags, mode)

    monkeypatch.setattr(command.os, "open", competing_open)
    assert scenario.run()["status"] == "already_attempted"
    assert scenario.tool_calls == []
    assert scenario.marker.read_text() == "other attempt"


def test_marker_flush_failure_prevents_tool_and_retains_no_retry_marker(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path)

    def fail_sync(_fd):
        raise OSError(PRIVATE)

    monkeypatch.setattr(command.os, "fsync", fail_sync)
    report = scenario.run()
    assert report["status"] == "blocked"
    assert report["reason"] == "marker_persistence_failed_do_not_retry"
    assert scenario.tool_calls == []
    assert scenario.marker.exists()
    assert scenario.run()["status"] == "already_attempted"
    assert PRIVATE not in json.dumps(report)


@pytest.mark.parametrize("requested", ["2026-09-31", "20260928", "private-invalid-date"])
def test_invalid_date_is_rejected_without_network(tmp_path, requested):
    scenario = Scenario(tmp_path)
    report = scenario.run(requested_date=requested)
    assert report == {"status": "blocked", "reason": "invalid_date"}
    assert scenario.health_calls == 0
    assert scenario.tool_calls == []


def test_cli_prints_only_sanitized_summary_and_never_loads_credentials(tmp_path, monkeypatch, capsys):
    scenario = Scenario(tmp_path)
    monkeypatch.setattr(command, "DEFAULT_MARKER", scenario.marker)
    monkeypatch.setattr(command, "fetch_live_health", scenario.fetch_health)
    monkeypatch.setattr(command, "call_live_stats", scenario.call_stats)
    # Freeze the core default clock too (default args are bound at definition).
    original_verify = command.verify_once

    async def frozen_verify(**kwargs):
        return await original_verify(**kwargs, now=lambda: NOW)

    monkeypatch.setattr(command, "verify_once", frozen_verify)
    monkeypatch.setenv("GARMIN_PASSWORD", PRIVATE)
    monkeypatch.setenv("GITHUB_TOKEN", PRIVATE)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", PRIVATE)
    assert command.main(["--date", DATE]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out)["status"] == "passed"
    assert PRIVATE not in output.out


def test_real_mcp_adapter_has_one_fixed_call_and_no_credential_or_redirect_loading(monkeypatch):
    import fastmcp
    import fastmcp.client.transports
    import httpx

    calls = []

    class FakeHttpClient:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
            assert kwargs["auth"] is None

    class FakeTransport:
        def __init__(self, url, httpx_client_factory):
            assert url == "https://garmin-mcp-server-kj2q.onrender.com/mcp"
            # Match the pinned FastMCP factory contract, including its redirect
            # default; the acceptance client must override that default safely.
            self.http = httpx_client_factory(headers={}, auth=None, follow_redirects=True, timeout=120)

    class FakeClient:
        def __init__(self, transport, timeout, init_timeout):
            assert isinstance(transport, FakeTransport)
            assert timeout == 120
            assert init_timeout == 30

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def call_tool_mcp(self, name, arguments, timeout):
            calls.append((name, arguments, timeout))
            return stats_result()

    monkeypatch.setattr(fastmcp, "Client", FakeClient)
    monkeypatch.setattr(fastmcp.client.transports, "StreamableHttpTransport", FakeTransport)
    monkeypatch.setattr(httpx, "AsyncClient", FakeHttpClient)
    result = asyncio.run(command.call_live_stats(DATE))
    assert command.valid_stats_result(result, DATE)
    assert calls == [("get_stats", {"date": DATE}, 120)]


def test_real_health_adapter_is_fixed_bounded_and_credential_free(monkeypatch):
    import httpx

    calls = []

    class FakeResponse:
        text = json.dumps(health())
        content = text.encode()

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, timeout, follow_redirects, trust_env):
            assert timeout.connect == 15
            assert timeout.read == 85
            assert follow_redirects is False
            assert trust_env is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            calls.append(url)
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    result = asyncio.run(command.fetch_live_health())
    assert result["ok"] is True
    assert calls == ["https://garmin-mcp-server-kj2q.onrender.com/health"]


@pytest.mark.parametrize("kind,expected", [
    ("read_timeout", "health_timeout"),
    ("total_timeout", "health_timeout"),
    ("connect", "health_connection_error"),
    ("http", "health_http_error"),
    ("json", "health_invalid_response"),
    ("unknown", "health_unknown_error"),
])
def test_health_failure_diagnostic_is_fixed_and_never_retried(tmp_path, kind, expected):
    import httpx

    request = httpx.Request("GET", command.HEALTH_URL)
    errors = {
        "read_timeout": httpx.ReadTimeout(PRIVATE),
        "total_timeout": TimeoutError(PRIVATE),
        "connect": httpx.ConnectError(PRIVATE),
        "http": httpx.HTTPStatusError(PRIVATE, request=request,
                                       response=httpx.Response(503, request=request)),
        "json": ValueError(PRIVATE),
        "unknown": RuntimeError(PRIVATE),
    }
    scenario = Scenario(tmp_path)
    scenario.health_error = errors[kind]
    report = scenario.run()
    assert report["health_error_code"] == expected
    assert report["status"] == "blocked"
    assert report["tool_calls"] == 0
    assert scenario.health_calls == 1
    assert not scenario.marker.exists()
    assert PRIVATE not in json.dumps(report)


def test_health_total_deadline_cancels_a_slow_response_without_retry(monkeypatch):
    import httpx

    calls = []

    class SlowClient:
        def __init__(self, **kwargs):
            assert kwargs["timeout"].read == 85

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            calls.append(url)
            await asyncio.sleep(1)
            raise AssertionError("deadline did not cancel the response")

    assert command.HEALTH_TOTAL_TIMEOUT_SECONDS == 90
    monkeypatch.setattr(command, "HEALTH_TOTAL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(httpx, "AsyncClient", SlowClient)
    with pytest.raises(TimeoutError):
        asyncio.run(command.fetch_live_health())
    assert calls == [command.HEALTH_URL]


def test_postflight_timeout_keeps_marker_and_does_not_repeat_garmin(tmp_path):
    scenario = Scenario(tmp_path)

    async def fetch():
        scenario.health_calls += 1
        if scenario.health_calls == 1:
            return health()
        raise TimeoutError(PRIVATE)

    scenario.fetch_health = fetch
    report = scenario.run()
    assert report["status"] == "attempted_unknown"
    assert report["health_error_code"] == "health_timeout"
    assert scenario.tool_calls == [DATE]
    assert PRIVATE not in scenario.marker.read_text()
    assert scenario.run()["status"] == "already_attempted"
    assert scenario.tool_calls == [DATE]
