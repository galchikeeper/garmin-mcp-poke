"""Exercise real upstream logging paths with fake errors and offline signing."""
import io
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import requests
from requests_oauthlib import OAuth1
from garminconnect import Garmin
from garth.data.body_battery.events import BodyBatteryData

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from safe_logging import suppress_upstream_logs


NAMESPACES = ("garminconnect", "garth", "requests_oauthlib", "oauthlib", "urllib3")


def is_upstream(name):
    return any(name == prefix or name.startswith(prefix + ".") for prefix in NAMESPACES)


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch):
    root = logging.getLogger()
    saved_filters = list(root.filters)
    saved = {}
    for name in set(logging.Logger.manager.loggerDict) | set(NAMESPACES):
        if not is_upstream(name):
            continue
        logger = logging.getLogger(name)
        saved[name] = (logger.disabled, logger.propagate, list(logger.handlers), logger.level)
        logger.disabled = False
        logger.propagate = True
        logger.handlers[:] = []
        logger.setLevel(logging.NOTSET)
    root.filters[:] = [item for item in root.filters
                       if not getattr(item, "_garmin_mcp_upstream_root_filter", False)]

    def forbid_network(*args, **kwargs):
        pytest.fail("logging regression tests must never make network requests")

    monkeypatch.setattr(requests.sessions.Session, "request", forbid_network)
    yield
    root.filters[:] = saved_filters
    for name in set(logging.Logger.manager.loggerDict):
        if not is_upstream(name):
            continue
        logger = logging.getLogger(name)
        disabled, propagate, handlers, level = saved.get(name, (False, True, [], logging.NOTSET))
        logger.disabled = disabled
        logger.propagate = propagate
        logger.handlers[:] = handlers
        logger.setLevel(level)


def test_real_garmin_exception_is_suppressed_but_app_logs_survive(caplog):
    caplog.set_level(logging.DEBUG)
    suppress_upstream_logs()
    client = Garmin()

    def fail(*args, **kwargs):
        raise requests.exceptions.ConnectionError("dummy-sensitive-upstream-error")

    client.garth.connectapi = fail
    with pytest.raises(Exception):
        client.connectapi("/dummy-health-path")
    logging.getLogger("bin_logging_test.application").info("safe application diagnostic")
    logging.warning("safe direct root diagnostic")
    assert "dummy-sensitive-upstream-error" not in caplog.text
    assert "dummy-health-path" not in caplog.text
    assert "safe application diagnostic" in caplog.text
    assert "safe direct root diagnostic" in caplog.text


def test_existing_child_handlers_and_future_child_propagation_are_suppressed(caplog):
    caplog.set_level(logging.DEBUG)
    existing = logging.getLogger("garminconnect.existing_sensitive_child")
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    existing.addHandler(handler)
    suppress_upstream_logs()
    existing.warning("dummy-existing-child-secret")
    for namespace in NAMESPACES:
        # Created after configuration: parent disabled alone would not stop it.
        logging.getLogger(namespace + ".future_sensitive_child").warning("dummy-future-child-secret")
    assert not sink.getvalue()
    assert "dummy-existing-child-secret" not in caplog.text
    assert "dummy-future-child-secret" not in caplog.text
    # Detached handlers remain usable by application code.
    handler.handle(logging.makeLogRecord({"msg": "shared handler still usable"}))
    assert "shared handler still usable" in sink.getvalue()


def test_garth_direct_root_exception_logging_is_suppressed(caplog):
    caplog.set_level(logging.DEBUG)
    suppress_upstream_logs()

    def fail(*args, **kwargs):
        raise RuntimeError("dummy-private-garmin-response")

    assert BodyBatteryData.get("2026-09-28", client=SimpleNamespace(connectapi=fail)) == []
    assert "dummy-private-garmin-response" not in caplog.text
    assert "Failed to fetch Body Battery events" not in caplog.text
    logging.getLogger("bin_logging_test.application").warning("application warning retained")
    assert "application warning retained" in caplog.text


def test_oauth_signing_never_logs_dummy_token_even_with_debug_enabled(caplog):
    caplog.set_level(logging.DEBUG)
    suppress_upstream_logs()
    request = requests.Request("GET", "https://example.invalid/dummy").prepare()
    signer = OAuth1("dummy-consumer-key", client_secret="dummy-consumer-secret",
                    resource_owner_key="dummy-oauth-token", resource_owner_secret="dummy-oauth-secret")
    signer(request)  # Signs in memory; no Session.request call.
    assert b"dummy-oauth-token" in request.headers["Authorization"]
    for secret in ("dummy-consumer-key", "dummy-consumer-secret", "dummy-oauth-token", "dummy-oauth-secret"):
        assert secret not in caplog.text
    assert "Updated headers" not in caplog.text
    assert "Collected params" not in caplog.text


def test_repeated_setup_keeps_one_filter_and_one_null_handler(caplog):
    caplog.set_level(logging.DEBUG)
    root = logging.getLogger()
    original_handlers = tuple(root.handlers)
    for _ in range(5):
        suppress_upstream_logs()
    assert tuple(root.handlers) == original_handlers
    assert len([item for item in root.filters
                if getattr(item, "_garmin_mcp_upstream_root_filter", False)]) == 1
    for namespace in NAMESPACES:
        logger = logging.getLogger(namespace)
        assert logger.disabled is True
        assert logger.propagate is False
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.NullHandler)
    logging.info("ordinary root info survives repeated setup")
    assert "ordinary root info survives repeated setup" in caplog.text
