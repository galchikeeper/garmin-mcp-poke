"""
Garmin Connect authentication wrapper for cloud deployment.

Design (rewritten 2026-09-10):
- Server startup is decoupled from Garmin authentication. The process stays
  alive even when auth fails.
- Auth happens on first use and is retried once on a 401/403 style error.
- Failures surface as exceptions the MCP tools turn into error text.
  The process never calls sys.exit().
"""
import functools
import io
import sys
import threading
import time

from garminconnect import Garmin, GarminConnectAuthenticationError

from config import GARMINTOKENS_BASE64, GARMIN_EMAIL, GARMIN_PASSWORD

try:  # garth only exists on the garminconnect 0.2.x line
    from garth.exc import GarthHTTPError
except Exception:  # pragma: no cover
    class GarthHTTPError(Exception):
        pass


AUTH_MARKERS = ("401", "403", "unauthorized", "forbidden", "token", "oauth", "login")


def _log(msg):
    print(f"[garmin] {msg}", file=sys.stderr, flush=True)


def _is_auth_error(exc):
    if isinstance(exc, (GarminConnectAuthenticationError, GarthHTTPError)):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in AUTH_MARKERS)


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
        self._lock = threading.Lock()
        self._last_error = None
        self._last_attempt = 0.0

    def _ensure(self, force=False):
        with self._lock:
            if force:
                self._client = None
            if self._client is not None:
                return self._client
            gap = time.time() - self._last_attempt
            if self._last_error is not None and gap < 20:
                raise RuntimeError(f"Garmin auth is failing: {self._last_error}")
            self._last_attempt = time.time()
            try:
                self._client = _authenticate()
                self._last_error = None
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
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
            try:
                return attr(*args, **kwargs)
            except Exception as exc:
                if not _is_auth_error(exc):
                    raise
                _log(f"auth error, re-authenticating: {type(exc).__name__}: {exc}")
                fresh = self._ensure(force=True)
                return getattr(fresh, name)(*args, **kwargs)

        return wrapper


def init_garmin_client():
    """Always returns the proxy. No network call happens here."""
    return GarminProxy()
