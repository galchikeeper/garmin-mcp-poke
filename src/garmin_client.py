"""
Garmin Connect authentication wrapper for cloud deployment.

Design:
- Server startup is decoupled from Garmin authentication. The process stays
  alive even when auth fails.
- Auth happens on first use and is retried once on a 401/403 style error.
- HTTP 429 pauses requests and never triggers immediate re-authentication.
- Failures surface as exceptions the MCP tools turn into error text.
  The process never calls sys.exit().
"""
import functools
import io
import math
import os
import re
import sys
import threading
import time
from email.utils import parsedate_to_datetime
from datetime import timezone

from garminconnect import Garmin, GarminConnectAuthenticationError

from config import GARMINTOKENS_BASE64, GARMIN_EMAIL, GARMIN_PASSWORD

AUTH_MARKERS = ("unauthorized", "forbidden")


class GarminRateLimitError(RuntimeError):
    """A local cooldown, not a promise about when Garmin's limit expires."""

    def __init__(self, retry_after_seconds):
        self.retry_after_seconds = max(1, math.ceil(retry_after_seconds))
        super().__init__(
            "Garmin returned 429. Requests are paused for "
            f"{self.retry_after_seconds} seconds; do not retry or re-authenticate "
            "before then. This is a local cooldown, not Garmin's reset time."
        )


def _exceptions(exc):
    pending, seen = [exc], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for key in ("__cause__", "__context__", "error"):
            nested = getattr(current, key, None)
            if isinstance(nested, BaseException):
                pending.append(nested)


def _is_rate_limit(exc):
    for current in _exceptions(exc):
        response = getattr(current, "response", None)
        if getattr(response, "status_code", None) == 429:
            return True
        text = f"{type(current).__name__} {current}".lower()
        if "toomanyrequests" in text or re.search(r"\b429\b", text):
            return True
    return isinstance(exc, GarminRateLimitError)


def _retry_after(exc):
    """Read Retry-After from the HTTP error, including wrapped exceptions."""
    delays = []
    for current in _exceptions(exc):
        response = getattr(current, "response", None)
        headers = getattr(response, "headers", {}) or {}
        value = headers.get("Retry-After", headers.get("retry-after"))
        if value is None:
            continue
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                stamp = parsedate_to_datetime(str(value))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                delay = stamp.timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(delay) and delay > 0:
            delays.append(delay)
    return max(delays) if delays else None


def _log(msg):
    print(f"[garmin] {msg}", file=sys.stderr, flush=True)


def _is_auth_error(exc):
    # GarthHTTPError also represents 429 and other non-authentication failures.
    # An OAuth URL in an error message does not justify another login.
    if _is_rate_limit(exc):
        return False
    if isinstance(exc, GarminConnectAuthenticationError):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return bool(re.search(r"\b(?:401|403)\b", text)) or any(m in text for m in AUTH_MARKERS)


def _auth_with_tokens(token_base64):
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        garmin = Garmin()
        garmin.login(token_base64)
    finally:
        sys.stderr = old_stderr
    _log("authenticated with tokens.")
    return garmin


def _auth_with_credentials(email, password):
    garmin = Garmin(email=email, password=password, is_cn=False)
    garmin.login()
    _log("authenticated with email and password.")
    return garmin


def _authenticate():
    """Perform the actual login. Exceptions propagate to the caller."""
    if GARMINTOKENS_BASE64:
        return _auth_with_tokens(GARMINTOKENS_BASE64)
    if GARMIN_EMAIL and GARMIN_PASSWORD:
        return _auth_with_credentials(GARMIN_EMAIL, GARMIN_PASSWORD)
    raise RuntimeError(
        "No Garmin credentials configured. Set GARMINTOKENS_BASE64 or "
        "GARMIN_EMAIL + GARMIN_PASSWORD."
    )


class GarminProxy:
    """The single handle every tool module holds.

    The inner client can be swapped out, so an expired token is re-authenticated
    without touching any module code.
    """

    def __init__(self):
        self._client = None
        self._lock = threading.RLock()
        self._last_error = None
        self._last_attempt = 0.0
        self._blocked_until = 0.0
        self._rate_limit_count = 0
        try:
            cooldown = float(os.getenv("GARMIN_RATE_LIMIT_COOLDOWN_SEC", "1800"))
        except ValueError:
            cooldown = 1800.0
        self._cooldown = cooldown if math.isfinite(cooldown) and cooldown > 0 else 1800.0

    def _check_cooldown(self):
        remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            raise GarminRateLimitError(remaining)

    def _record_rate_limit(self, exc):
        self._rate_limit_count += 1
        fallback = min(self._cooldown * (2 ** min(self._rate_limit_count - 1, 10)), 7200)
        # Never shorten a longer delay explicitly supplied by Garmin.
        delay = max(fallback, _retry_after(exc) or 0)
        self._blocked_until = time.monotonic() + delay
        error = GarminRateLimitError(delay)
        self._last_error = str(error)
        _log(str(error))
        return error

    def _ensure(self, force=False):
        with self._lock:
            self._check_cooldown()
            if force:
                self._client = None
            if self._client is not None:
                return self._client
            gap = time.monotonic() - self._last_attempt
            if self._last_error is not None and gap < 20:
                raise RuntimeError(f"Garmin auth is failing: {self._last_error}")
            self._last_attempt = time.monotonic()
            try:
                self._client = _authenticate()
                self._last_error = None
            except Exception as exc:
                if _is_rate_limit(exc):
                    raise self._record_rate_limit(exc) from exc
                self._last_error = f"{type(exc).__name__}: Garmin authentication failed"
                _log(f"auth failed: {self._last_error}")
                raise
            return self._client

    def warmup(self):
        """Authenticate in the background so the first tool call is fast."""
        try:
            self._ensure()
            _log("warmup done.")
        except Exception:
            _log("warmup failed - will retry on first tool call.")

    def status(self):
        return {
            "authenticated": self._client is not None,
            "last_error": self._last_error,
            "retry_after_seconds": max(0, math.ceil(self._blocked_until - time.monotonic())),
        }

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        client = self._ensure()
        attr = getattr(client, name)
        if not callable(attr):
            return attr

        @functools.wraps(attr)
        def wrapper(*args, **kwargs):
            # Serialize requests and refreshes from all clients sharing this process.
            # Re-check here as callers can retain a method before a cooldown begins.
            with self._lock:
                client = self._ensure()
                try:
                    result = getattr(client, name)(*args, **kwargs)
                except Exception as exc:
                    if _is_rate_limit(exc):
                        raise self._record_rate_limit(exc) from exc
                    if not _is_auth_error(exc):
                        raise
                    _log(f"auth error, re-authenticating: {type(exc).__name__}")
                    fresh = self._ensure(force=True)
                    try:
                        result = getattr(fresh, name)(*args, **kwargs)
                    except Exception as retry_exc:
                        if _is_rate_limit(retry_exc):
                            raise self._record_rate_limit(retry_exc) from retry_exc
                        raise
                self._rate_limit_count = 0
                self._last_error = None
                return result

        return wrapper


def init_garmin_client():
    """Always returns the proxy. No network call happens here."""
    return GarminProxy()
