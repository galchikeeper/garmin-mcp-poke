"""Offline regression tests for authentication, persistence and request limits."""
import base64
import copy
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from garmin_client import BACKOFF, RECOVERY_HOLD, GarminProxy, GarminUnavailable, error_details, iso
from token_store import TokenStoreError


@pytest.mark.parametrize("timestamp", ["invalid", float("nan"), float("inf"), -1, 1e100, True])
def test_malformed_token_expiry_does_not_break_health(timestamp):
    assert iso(timestamp) is None


class Clock:
    def __init__(self, now=1_800_000_000):
        self.now = now

    def __call__(self):
        return self.now


class MemoryStore:
    def __init__(self, state=None):
        self.state = copy.deepcopy(state or {})
        self.loads = 0
        self.saves = []
        self.load_error = False
        self.save_error = False
        self.fail_save_at = None
        self.save_attempts = 0

    def load(self):
        self.loads += 1
        if self.load_error:
            raise TokenStoreError("TEST_STORE_READ_FAILED")
        return copy.deepcopy(self.state)

    def save(self, state):
        self.save_attempts += 1
        if self.save_error or self.save_attempts == self.fail_save_at:
            raise TokenStoreError("TEST_STORE_WRITE_FAILED")
        self.state = copy.deepcopy(state)
        self.saves.append(copy.deepcopy(state))


class FakeGarth:
    def __init__(self):
        self.tokens = None
        self.configurations = []
        self.loaded = []
        self.refresh_calls = 0
        self.oauth2_token = SimpleNamespace(expires_at=1_900_000_000)

    def configure(self, **kwargs):
        self.configurations.append(kwargs)

    def loads(self, tokens):
        self.loaded.append(tokens)
        self.tokens = tokens

    def dumps(self):
        return self.tokens

    def refresh_oauth2(self):
        self.refresh_calls += 1
        self.tokens = "refreshed-test-tokens"


class FakeGarmin:
    def __init__(self):
        self.garth = FakeGarth()
        self.login_calls = []
        self.api_calls = []
        self.login_errors = []
        self.api_error = None
        self.refresh_on_login = False
        self.login_entered = None
        self.login_release = None
        self.inspect_before_api = None

    def login(self, tokens):
        self.login_calls.append(tokens)
        if self.login_entered:
            self.login_entered.set()
            assert self.login_release.wait(timeout=5), "test login was not released"
        if self.login_errors:
            raise self.login_errors.pop(0)
        if self.refresh_on_login:
            self.garth.refresh_oauth2()

    def get_stats(self, date):
        if self.inspect_before_api:
            self.inspect_before_api()
        self.api_calls.append(date)
        if self.api_error:
            raise self.api_error
        return {"date": date, "steps": 123}


class Factory:
    def __init__(self, client=None):
        self.client = client or FakeGarmin()
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.client


def http_error(status, retry_after=None):
    error = RuntimeError("private upstream response must never escape")
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    error.response = SimpleNamespace(status_code=status, headers=headers)
    return error


@pytest.fixture
def setup_proxy():
    clock = Clock()
    store = MemoryStore({"tokens": "persisted-test-tokens"})
    factory = Factory()
    proxy = GarminProxy(store=store, seed="environment-test-tokens", factory=factory, clock=clock)
    return proxy, store, factory, clock


def test_status_has_no_io_and_warmup_does_not_authenticate(setup_proxy):
    proxy, store, factory, _ = setup_proxy
    assert proxy.status()["tokens_loaded"] is False
    assert store.loads == 0
    proxy.warmup()
    for _ in range(10):
        status = proxy.status()
        assert status["tokens_loaded"] is True
        assert status["authenticated"] is False
        assert status["server_password_login_enabled"] is False
    proxy.warmup()
    assert store.loads == 1
    assert store.saves == []
    assert factory.calls == [((), {})]
    assert factory.client.login_calls == []
    assert factory.client.api_calls == []
    assert factory.client.garth.configurations == [{"retries": 0, "timeout": 20}]
    assert "persisted-test-tokens" not in json.dumps(status)


def test_persisted_tokens_take_priority_over_environment(setup_proxy):
    proxy, _, factory, _ = setup_proxy
    proxy.warmup()
    assert factory.client.garth.loaded == ["persisted-test-tokens"]
    assert proxy.status()["token_source"] == "gist"
    assert proxy.get_stats("2026-09-28")["steps"] == 123
    assert factory.client.login_calls == ["persisted-test-tokens"]


def test_empty_store_uses_environment_without_losing_cooldown():
    clock = Clock()
    until = clock() + 21_600
    store = MemoryStore({"failure_count": 2, "login_cooldown_until": until,
                         "last_login_at": clock() - 1000})
    factory = Factory()
    proxy = GarminProxy(store=store, seed="environment-test-tokens", factory=factory, clock=clock)
    proxy.warmup()
    assert store.state["tokens"] == "environment-test-tokens"
    assert store.state["failure_count"] == 2
    assert store.state["login_cooldown_until"] == until
    assert proxy.status()["token_source"] == "environment"
    with pytest.raises(GarminUnavailable, match="GARMIN_COOLDOWN_UNTIL"):
        proxy.get_stats("2026-09-28")
    assert factory.client.login_calls == []
    assert factory.client.api_calls == []


def test_429_persists_six_hour_minimum_across_restart(setup_proxy):
    proxy, store, factory, clock = setup_proxy
    factory.client.login_errors = [http_error(429, "30")]
    with pytest.raises(GarminUnavailable, match="^GARMIN_RATE_LIMITED$"):
        proxy.get_stats("2026-09-28")
    assert store.state["login_cooldown_until"] == clock() + 6 * 60 * 60
    assert store.state["failure_count"] == 1
    assert store.state["last_rate_limit_at"] == clock()
    restarted_factory = Factory()
    restarted = GarminProxy(store=store, seed="older-seed", factory=restarted_factory, clock=clock)
    restarted.warmup()
    with pytest.raises(GarminUnavailable, match="GARMIN_COOLDOWN_UNTIL"):
        restarted.get_stats("2026-09-28")
    assert restarted_factory.client.login_calls == []
    assert restarted_factory.client.api_calls == []
    clock.now += 6 * 60 * 60
    assert restarted.get_stats("2026-09-28")["steps"] == 123
    assert store.state["failure_count"] == 0
    assert store.state["login_cooldown_until"] == 0


@pytest.mark.parametrize("use_date", [False, True])
def test_retry_after_longer_than_minimum_is_respected(setup_proxy, use_date):
    proxy, store, factory, clock = setup_proxy
    wait = 8 * 60 * 60
    retry = format_datetime(datetime.fromtimestamp(clock() + wait, timezone.utc), usegmt=True) if use_date else str(wait)
    outer = RuntimeError("wrapped")
    outer.error = http_error(429, retry)
    factory.client.login_errors = [outer]
    with pytest.raises(GarminUnavailable, match="GARMIN_RATE_LIMITED"):
        proxy.get_stats("2026-09-28")
    assert store.state["login_cooldown_until"] == clock() + wait


def test_generic_failures_escalate_15_minutes_one_hour_six_hours_one_day(setup_proxy):
    proxy, store, factory, clock = setup_proxy
    factory.client.login_errors = [RuntimeError("secret") for _ in range(5)]
    for attempt, wait in enumerate((*BACKOFF, BACKOFF[-1]), start=1):
        with pytest.raises(GarminUnavailable, match="^GARMIN_REQUEST_FAILED$"):
            proxy.get_stats("2026-09-28")
        assert store.state["failure_count"] == attempt
        assert store.state["login_cooldown_until"] == clock() + wait
        assert len(factory.client.login_calls) == attempt
        with pytest.raises(GarminUnavailable, match="GARMIN_COOLDOWN_UNTIL"):
            proxy.get_stats("2026-09-28")
        assert len(factory.client.login_calls) == attempt
        clock.now += wait
    assert factory.client.api_calls == []


def test_repeated_api_429_escalates_despite_successful_profile_bootstrap(setup_proxy):
    proxy, store, factory, clock = setup_proxy
    factory.client.api_error = http_error(429)
    for attempt, wait in enumerate((21_600, 21_600, 21_600, 86_400), start=1):
        with pytest.raises(GarminUnavailable, match="^GARMIN_RATE_LIMITED$"):
            proxy.get_stats("2026-09-28")
        assert store.state["failure_count"] == attempt
        assert store.state["login_cooldown_until"] == clock() + wait
        assert len(factory.client.login_calls) == attempt
        assert len(factory.client.api_calls) == attempt
        assert store.state["last_success_at"] is None
        clock.now += wait
    factory.client.api_error = None
    assert proxy.get_stats("2026-09-28")["steps"] == 123
    assert store.state["failure_count"] == 0
    assert store.state["login_cooldown_until"] == 0
    assert store.state["last_success_at"] == clock()


@pytest.mark.parametrize("retry_after", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_nonfinite_retry_after_is_ignored(setup_proxy, retry_after):
    proxy, store, factory, clock = setup_proxy
    factory.client.login_errors = [http_error(429, retry_after)]
    with pytest.raises(GarminUnavailable, match="^GARMIN_RATE_LIMITED$"):
        proxy.get_stats("2026-09-28")
    assert store.state["login_cooldown_until"] == clock() + 21_600
    assert proxy.status()["login_cooldown_until"] == iso(clock() + 21_600)


def test_concurrent_calls_share_one_client_and_one_authentication(setup_proxy):
    proxy, store, factory, _ = setup_proxy
    client = factory.client
    client.login_entered = threading.Event()
    client.login_release = threading.Event()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(proxy.get_stats, "2026-09-28")]
        assert client.login_entered.wait(timeout=5)
        futures.extend(executor.submit(proxy.get_stats, "2026-09-28") for _ in range(7))
        # Health remains nonblocking even while the network operation holds the lock.
        assert proxy.status()["authenticated"] is False
        client.login_release.set()
        assert all(future.result(timeout=5)["steps"] == 123 for future in futures)
    assert len(factory.calls) == 1
    assert len(client.login_calls) == 1
    assert len(client.api_calls) == 8
    assert store.loads == 1


def test_refreshed_tokens_are_saved_immediately_before_api_request(setup_proxy):
    proxy, store, factory, clock = setup_proxy
    factory.client.refresh_on_login = True

    def assert_persisted():
        assert store.state["tokens"] == "refreshed-test-tokens"
        assert store.state["last_refresh_at"] == clock()
        refresh_writes = [state for state in store.saves if state.get("last_refresh_at")]
        assert refresh_writes[0]["tokens"] == "refreshed-test-tokens"
        # Refresh and successful profile bootstrap retain the crash-recovery
        # guard; only successful completion of the requested tool clears it.
        assert refresh_writes[0]["login_cooldown_until"] == clock() + RECOVERY_HOLD
        assert store.state["login_cooldown_until"] == clock() + RECOVERY_HOLD

    factory.client.inspect_before_api = assert_persisted
    assert proxy.get_stats("2026-09-28")["steps"] == 123
    assert factory.client.garth.refresh_calls == 1
    assert store.state["login_cooldown_until"] == 0


@pytest.mark.parametrize("status,code", [(401, "GARMIN_AUTH_REJECTED_LOCAL_RESEED_REQUIRED"),
                                        (503, "GARMIN_REQUEST_FAILED")])
def test_api_error_does_not_replay_request_or_retry_login(setup_proxy, status, code):
    proxy, store, factory, _ = setup_proxy
    factory.client.api_error = http_error(status)
    with pytest.raises(GarminUnavailable, match=f"^{code}$"):
        proxy.get_stats("2026-09-28")
    with pytest.raises(GarminUnavailable, match="GARMIN_COOLDOWN_UNTIL"):
        proxy.get_stats("2026-09-28")
    assert len(factory.client.login_calls) == 1
    assert factory.client.api_calls == ["2026-09-28"]
    assert store.state["failure_count"] == 1
    assert proxy.status()["authenticated"] is False


def test_restart_and_token_restore_do_not_change_last_login(setup_proxy):
    proxy, store, _, clock = setup_proxy
    original_login = clock() - 86_400
    store.state["last_login_at"] = original_login
    proxy.get_stats("2026-09-28")
    clock.now += 100
    restarted = GarminProxy(store=store, factory=Factory(), clock=clock)
    restarted.warmup()
    assert restarted.status()["last_login_at"] == iso(original_login)
    restarted.get_stats("2026-09-28")
    assert store.state["last_login_at"] == original_login
    assert store.state["last_success_at"] == clock()


def test_store_read_failure_never_uses_seed_or_contacts_garmin(setup_proxy):
    proxy, store, factory, clock = setup_proxy
    original = copy.deepcopy(store.state)
    store.load_error = True
    proxy.warmup()
    assert proxy.status()["persistence_error"] == "TEST_STORE_READ_FAILED"
    for _ in range(3):
        with pytest.raises(GarminUnavailable, match="TOKEN_STORE_UNAVAILABLE"):
            proxy.get_stats("2026-09-28")
    assert store.loads == 1
    assert store.state == original
    assert store.save_attempts == 0
    assert factory.calls == []
    clock.now += 60
    store.load_error = False
    assert proxy.get_stats("2026-09-28")["steps"] == 123
    assert factory.client.garth.loaded == ["persisted-test-tokens"]


def test_ready_client_429_and_failed_save_retains_restart_guard(setup_proxy):
    proxy, store, factory, clock = setup_proxy
    proxy.get_stats("first-success")
    assert store.state["login_cooldown_until"] == 0

    def fail_storage_after_request_begins():
        assert store.state["login_cooldown_until"] == clock() + RECOVERY_HOLD
        store.save_error = True

    factory.client.inspect_before_api = fail_storage_after_request_begins
    factory.client.api_error = http_error(429)
    with pytest.raises(GarminUnavailable, match="^GARMIN_RATE_LIMITED$"):
        proxy.get_stats("second-429")
    assert len(factory.client.login_calls) == 1
    assert store.state["login_cooldown_until"] == clock() + RECOVERY_HOLD
    restarted_factory = Factory()
    restarted = GarminProxy(store=store, factory=restarted_factory, clock=clock)
    with pytest.raises(GarminUnavailable, match="GARMIN_COOLDOWN_UNTIL"):
        restarted.get_stats("must-not-run")
    assert restarted_factory.client.login_calls == []
    assert restarted_factory.client.api_calls == []


def test_ready_client_store_outage_blocks_request_before_garmin(setup_proxy):
    proxy, store, factory, _ = setup_proxy
    proxy.get_stats("first-success")
    store.save_error = True
    with pytest.raises(GarminUnavailable, match="TOKEN_STORE_UNAVAILABLE"):
        proxy.get_stats("must-not-run")
    assert factory.client.api_calls == ["first-success"]
    assert len(factory.client.login_calls) == 1


@pytest.mark.parametrize("method", ["get", "post"])
def test_existing_raw_workout_requests_are_guarded_without_exposing_tokens(setup_proxy, method):
    proxy, store, factory, clock = setup_proxy
    calls = []
    response = SimpleNamespace(status_code=200)

    def raw_request(*args, **kwargs):
        assert store.state["login_cooldown_until"] >= clock() + RECOVERY_HOLD
        calls.append((args, kwargs))
        return response

    setattr(factory.client.garth, method, raw_request)
    assert getattr(proxy.garth, method)("connectapi", "workout-service/test") is response
    assert calls == [(("connectapi", "workout-service/test"), {})]
    assert store.state["login_cooldown_until"] == 0
    for forbidden in ("login", "loads", "dumps", "refresh_oauth2", "oauth1_token", "oauth2_token"):
        with pytest.raises(AttributeError):
            getattr(proxy.garth, forbidden)


def test_raw_workout_write_failure_is_not_replayed_and_persists_cooldown(setup_proxy):
    proxy, store, factory, clock = setup_proxy
    calls = []

    def raw_post(*args, **kwargs):
        calls.append((args, kwargs))
        raise http_error(429)

    factory.client.garth.post = raw_post
    with pytest.raises(GarminUnavailable, match="^GARMIN_RATE_LIMITED$"):
        proxy.garth.post("connectapi", "workout-service/schedule/1", json={"date": "2026-09-28"})
    with pytest.raises(GarminUnavailable, match="GARMIN_COOLDOWN_UNTIL"):
        proxy.garth.post("connectapi", "workout-service/schedule/1", json={"date": "2026-09-28"})
    assert len(calls) == 1
    assert store.state["login_cooldown_until"] == clock() + RECOVERY_HOLD


def test_save_failure_blocks_authentication_and_request(setup_proxy):
    proxy, store, factory, clock = setup_proxy
    store.save_error = True
    with pytest.raises(GarminUnavailable, match="TOKEN_STORE_UNAVAILABLE"):
        proxy.get_stats("2026-09-28")
    assert factory.client.login_calls == []
    assert factory.client.api_calls == []
    assert proxy.status()["persistence_error"] == "TEST_STORE_WRITE_FAILED"
    clock.now += RECOVERY_HOLD
    with pytest.raises(GarminUnavailable, match="TOKEN_STORE_UNAVAILABLE"):
        proxy.get_stats("2026-09-28")
    assert factory.client.login_calls == []
    assert factory.client.api_calls == []
    store.save_error = False
    assert proxy.get_stats("2026-09-28")["steps"] == 123


def test_failed_refresh_save_stops_before_api_data_fetch(setup_proxy):
    proxy, store, factory, clock = setup_proxy
    factory.client.refresh_on_login = True
    store.fail_save_at = 3  # pre-login, pre-exchange, post-exchange
    with pytest.raises(GarminUnavailable, match="TOKEN_STORE_UNAVAILABLE"):
        proxy.get_stats("2026-09-28")
    assert factory.client.garth.refresh_calls == 1
    assert factory.client.api_calls == []
    assert proxy.status()["authenticated"] is False
    assert store.state["login_cooldown_until"] == clock() + RECOVERY_HOLD


def test_429_state_write_failure_still_leaves_durable_six_hour_guard(setup_proxy):
    proxy, store, factory, clock = setup_proxy
    store.fail_save_at = 2
    factory.client.login_errors = [http_error(429)]
    with pytest.raises(GarminUnavailable, match="GARMIN_RATE_LIMITED"):
        proxy.get_stats("2026-09-28")
    restarted_factory = Factory()
    restarted = GarminProxy(store=store, factory=restarted_factory, clock=clock)
    with pytest.raises(GarminUnavailable, match="GARMIN_COOLDOWN_UNTIL"):
        restarted.get_stats("2026-09-28")
    assert store.state["login_cooldown_until"] == clock() + RECOVERY_HOLD
    assert restarted_factory.client.login_calls == []


def test_missing_store_blocks_environment_fallback():
    factory = Factory()
    proxy = GarminProxy(store=None, seed="environment-test-tokens", factory=factory, clock=Clock())
    proxy.warmup()
    with pytest.raises(GarminUnavailable, match="TOKEN_STORE_UNAVAILABLE"):
        proxy.get_stats("2026-09-28")
    assert factory.calls == []
    assert proxy.status()["persistence_error"] == "TOKEN_STORE_NOT_CONFIGURED"


def test_missing_tokens_require_local_seed_without_login():
    factory = Factory()
    proxy = GarminProxy(store=MemoryStore(), factory=factory, clock=Clock())
    with pytest.raises(GarminUnavailable, match="LOCAL_TOKEN_SEED_REQUIRED"):
        proxy.get_stats("2026-09-28")
    assert factory.calls == []


def test_nested_rate_limit_details_handle_causal_cycle():
    root = RuntimeError("outer")
    nested = http_error(429, "36000")
    root.error = nested
    nested.__cause__ = root
    assert error_details(root, Clock()()) == (429, 36000)


def test_pinned_garth_loads_tokens_offline(monkeypatch):
    from garth.http import Client
    import requests

    def forbid_network(*args, **kwargs):
        pytest.fail("garth.loads unexpectedly attempted network access")

    monkeypatch.setattr(requests.sessions.Session, "request", forbid_network)
    dummy = [
        {"oauth_token": "dummy", "oauth_token_secret": "dummy", "domain": "garmin.com"},
        {"scope": "dummy", "jti": "dummy", "token_type": "Bearer", "access_token": "dummy",
         "refresh_token": "dummy", "expires_in": 3600, "expires_at": 1_900_000_000,
         "refresh_token_expires_in": 7200, "refresh_token_expires_at": 1_900_003_600},
    ]
    client = Client()
    client.loads(base64.b64encode(json.dumps(dummy).encode()).decode())
    assert client.oauth1_token.oauth_token == "dummy"
    assert client.oauth2_token.expires_at == 1_900_000_000
