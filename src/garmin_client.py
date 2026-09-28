"""Restart-safe Garmin proxy. Server processes never perform password login.

Pinned garth loads() is offline. Garmin.login(tokenstore) also fetches
profile/settings, so it runs only on a real MCP call, never on /health.
"""
import functools
import math
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectTooManyRequestsError
from config import GARMINTOKENS_BASE64, GIST_ID, GITHUB_TOKEN, TOKEN_ENCRYPTION_KEY
from token_store import TokenStore, TokenStoreError
from safe_logging import suppress_upstream_logs

# Upstream login, OAuth signing and Garth parsers can log credentials/payloads.
suppress_upstream_logs()

BACKOFF = (15 * 60, 60 * 60, 6 * 60 * 60, 24 * 60 * 60)
RECOVERY_HOLD = 6 * 60 * 60


class GarminUnavailable(RuntimeError):
    """Safe error for MCP clients; never embeds upstream data."""


class _GuardedGarth:
    """Keep the two existing raw workout HTTP helpers behind the proxy guard."""

    def __init__(self, proxy):
        self._proxy = proxy

    def get(self, *args, **kwargs):
        return self._proxy._invoke("get", args, kwargs, raw_garth=True)

    def post(self, *args, **kwargs):
        return self._proxy._invoke("post", args, kwargs, raw_garth=True)


def iso(timestamp):
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or not 0 < timestamp <= 253402300799:
        return None
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def error_details(exc, now):
    """Find status/Retry-After through garth's .error and causal chains."""
    pending, seen = [exc], set()
    status, retry_after = None, 0
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, GarminConnectTooManyRequestsError):
            status = 429
        response = getattr(current, "response", None)
        code = getattr(response, "status_code", None)
        if code == 429 or (status is None and isinstance(code, int)):
            status = code
        value = getattr(response, "headers", {}).get("Retry-After") if response is not None else None
        if value:
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                try:
                    seconds = parsedate_to_datetime(value).timestamp() - now
                except (TypeError, ValueError, OverflowError):
                    seconds = 0
            if math.isfinite(seconds):
                retry_after = max(retry_after, seconds)
        for field in ("error", "__cause__", "__context__"):
            child = getattr(current, field, None)
            if isinstance(child, BaseException):
                pending.append(child)
    return status, retry_after


class GarminProxy:
    """One process, one client, one serialized refresh; durable failure state."""

    def __init__(self, store=None, seed=None, factory=Garmin, clock=time.time):
        self._store = store
        self._seed = seed
        self._factory = factory
        self._clock = clock
        self._started = clock()
        self._lock = threading.RLock()
        self._client = None
        self._guarded_garth = _GuardedGarth(self)
        self._restored = False
        self._ready = False
        self._source = None
        self._persistence_error = None
        self._load_retry_at = 0
        self._dirty = False
        self._state = {"version": 1, "tokens": None, "failure_count": 0,
                       "login_cooldown_until": 0, "last_login_at": None,
                       "last_success_at": None, "last_error": None}

    def _save(self):
        self._dirty = True
        if self._store is None:
            self._persistence_error = "TOKEN_STORE_NOT_CONFIGURED"
            raise GarminUnavailable(self._persistence_error)
        try:
            self._store.save(self._state)
        except TokenStoreError as exc:
            self._persistence_error = exc.code
            raise GarminUnavailable("TOKEN_STORE_UNAVAILABLE") from None
        self._dirty = False
        self._persistence_error = None

    def _restore(self):
        if self._restored:
            return
        if self._clock() < self._load_retry_at:
            raise GarminUnavailable("TOKEN_STORE_UNAVAILABLE")
        try:
            if self._store is None:
                raise GarminUnavailable("TOKEN_STORE_NOT_CONFIGURED")
            loaded = self._store.load()
            self._state.update(loaded)
            self._source = "gist" if loaded.get("tokens") else None
        except (TokenStoreError, GarminUnavailable) as exc:
            # A stale seed must not overwrite a cooldown we failed to read.
            self._persistence_error = getattr(exc, "code", "TOKEN_STORE_NOT_CONFIGURED")
            self._load_retry_at = self._clock() + 60
            raise GarminUnavailable("TOKEN_STORE_UNAVAILABLE") from None
        if not self._state.get("tokens") and self._seed:
            self._state["tokens"] = self._seed
            self._source = "environment"
        if not self._state.get("tokens"):
            self._restored = True
            self._state["last_error"] = "LOCAL_TOKEN_SEED_REQUIRED"
            return
        try:
            client = self._factory()  # deliberately never receives credentials
            client.garth.configure(retries=0, timeout=20)
            client.garth.loads(self._state["tokens"])
        except Exception:
            self._state["last_error"] = "INVALID_TOKEN_SEED"
            self._restored = True
            raise GarminUnavailable("INVALID_TOKEN_SEED") from None

        original_refresh = client.garth.refresh_oauth2

        def refresh_and_persist():
            with self._lock:
                # Persist before exchange: a crash cannot cause an immediate
                # exchange loop after restart. Outer calls are serialized.
                self._arm_recovery_guard()
                original_refresh()
                self._state["tokens"] = client.garth.dumps()
                self._state["last_refresh_at"] = self._clock()
                self._save()  # immediately persist before fetching API data

        client.garth.refresh_oauth2 = refresh_and_persist
        self._client = client
        self._restored = True
        if self._source == "environment":
            self._save()

    def _check_cooldown(self):
        if self._clock() < self._state.get("login_cooldown_until", 0):
            raise GarminUnavailable("GARMIN_COOLDOWN_UNTIL " + iso(self._state["login_cooldown_until"]))

    def _arm_recovery_guard(self):
        # Even a ready client's data request can return 429. If recording that
        # failure subsequently fails, a restarted process must still wait.
        delay = max(RECOVERY_HOLD, BACKOFF[min(self._state.get("failure_count", 0), 3)])
        until = self._clock() + delay
        if self._dirty or self._state.get("login_cooldown_until", 0) < until:
            self._state["login_cooldown_until"] = max(
                self._state.get("login_cooldown_until", 0), until)
            self._save()

    def _failure(self, exc):
        status, retry_after = error_details(exc, self._clock())
        count = self._state.get("failure_count", 0) + 1
        wait = max(BACKOFF[min(count - 1, 3)], retry_after)
        if status == 429:
            wait = max(wait, 6 * 60 * 60)
            code = "GARMIN_RATE_LIMITED"
        elif status in (401, 403) or (status is None and isinstance(exc, GarminConnectAuthenticationError)):
            code = "GARMIN_AUTH_REJECTED_LOCAL_RESEED_REQUIRED"
        else:
            code = "GARMIN_REQUEST_FAILED"
        self._state.update(failure_count=count, last_error=code,
                           login_cooldown_until=self._clock() + wait)
        if status == 429:
            self._state["last_rate_limit_at"] = self._clock()
        self._ready = False
        try:
            self._save()
        except GarminUnavailable:
            pass  # memory cooldown remains; dirty state must save before retry
        return code

    def _success(self):
        self._state.update(tokens=self._client.garth.dumps(), failure_count=0,
                           login_cooldown_until=0, last_success_at=self._clock(), last_error=None)
        self._save()
        self._ready = True

    def _ensure(self):
        self._restore()
        self._check_cooldown()
        if self._dirty:
            self._save()
        if self._client is None:
            raise GarminUnavailable(self._state.get("last_error") or "LOCAL_TOKEN_SEED_REQUIRED")
        if not self._ready:
            self._arm_recovery_guard()
            try:
                # Pinned API restores tokens then obtains profile/settings.
                # No credentials exist in this client for password fallback.
                self._client.login(self._client.garth.dumps())
            except GarminUnavailable:
                raise
            except Exception as exc:
                raise GarminUnavailable(self._failure(exc)) from None
            # Profile bootstrap is not success of the requested tool. Preserve
            # the failure streak until that tool actually succeeds, otherwise
            # repeated data-API 429s would never escalate beyond attempt one.
            self._state["tokens"] = self._client.garth.dumps()
            self._save()
            self._ready = True
        return self._client

    def warmup(self):
        """Retrieve/decrypt saved state only. Never contact Garmin here."""
        with self._lock:
            try:
                self._restore()
            except GarminUnavailable:
                pass

    def status(self):
        # No I/O or network-held lock: health remains responsive. OAuth1 has no
        # expiry field in this library; MFA expiry is not OAuth1 expiry.
        state = self._state.copy()
        oauth2 = getattr(getattr(self._client, "garth", None), "oauth2_token", None)
        return {
            "authenticated": self._ready,
            "tokens_loaded": self._client is not None,
            "token_source": self._source,
            "oauth1_expires_at": None,
            "oauth2_expires_at": iso(getattr(oauth2, "expires_at", None)),
            "last_login_at": iso(state.get("last_login_at")),
            "last_success_at": iso(state.get("last_success_at")),
            "last_refresh_at": iso(state.get("last_refresh_at")),
            "last_rate_limit_at": iso(state.get("last_rate_limit_at")),
            "login_cooldown_until": iso(state.get("login_cooldown_until")),
            "failure_count": state.get("failure_count", 0),
            "uptime_seconds": max(0, int(self._clock() - self._started)),
            "persistence_error": self._persistence_error,
            "last_error": state.get("last_error"),
            "server_password_login_enabled": False,
        }

    def _invoke(self, name, args, kwargs, raw_garth=False):
        with self._lock:
            client = self._ensure()
            self._arm_recovery_guard()
            try:
                target = client.garth if raw_garth else client
                result = getattr(target, name)(*args, **kwargs)
            except GarminUnavailable:
                raise
            except Exception as exc:
                # Don't replay tools; writes may have succeeded before
                # their response was lost. No auth-error retry loop.
                raise GarminUnavailable(self._failure(exc)) from None
            self._success()
            return result

    def __getattr__(self, name):
        if name == "garth":
            return self._guarded_garth
        if name.startswith("_") or name in ("login", "logout"):
            raise AttributeError(name)
        if name in ("display_name", "full_name", "unit_system"):
            with self._lock:
                value = getattr(self._ensure(), name)
                self._success()
                return value

        @functools.wraps(getattr(Garmin, name, lambda: None))
        def wrapper(*args, **kwargs):
            return self._invoke(name, args, kwargs)
        return wrapper


def init_garmin_client():
    store = None
    if GIST_ID and GITHUB_TOKEN and TOKEN_ENCRYPTION_KEY:
        try:
            store = TokenStore(GIST_ID, GITHUB_TOKEN, TOKEN_ENCRYPTION_KEY)
        except TokenStoreError:
            pass
    return GarminProxy(store=store, seed=GARMINTOKENS_BASE64)
