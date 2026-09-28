"""Token-only Garmin sessions with durable rate limiting; no unattended SSO login.

Pinned to garminconnect 0.3.16 because the small native-client adapter below
uses its refresh/transport hooks. Test these hooks before updating the pin.
"""
import functools
import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from garminconnect import Garmin, GarminConnectAuthenticationError
from garminconnect.client import Client
from state_store import StateStore, StorageError, atomic_json, read_json


class GarminRateLimitError(RuntimeError):
    def __init__(self, retry_after_seconds):
        self.retry_after_seconds = max(1, math.ceil(retry_after_seconds))
        super().__init__(
            f"Garmin returned 429. Requests are paused for {self.retry_after_seconds} "
            "seconds. Do not retry or re-authenticate before then. "
            "This is a local cooldown, not Garmin's reset time."
        )


class GarminSetupError(RuntimeError):
    pass


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
        if isinstance(current, GarminRateLimitError):
            return True
        if getattr(getattr(current, "response", None), "status_code", None) == 429:
            return True
        text = f"{type(current).__name__} {current}".lower()
        if "toomanyrequests" in text or re.search(r"\b429\b", text):
            return True
    return False


def _retry_after(exc):
    delays = []
    for current in _exceptions(exc):
        headers = getattr(getattr(current, "response", None), "headers", {}) or {}
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


def _is_auth_error(exc):
    if _is_rate_limit(exc):
        return False
    return any(
        isinstance(current, GarminConnectAuthenticationError)
        or re.search(r"\b(?:401|403)\b", str(current))
        for current in _exceptions(exc)
    )


def validate_tokens(data):
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), str) and data[key].strip()
        for key in ("di_token", "di_refresh_token", "di_client_id")
    ):
        raise GarminSetupError(
            "Modern DI tokens are required. Run scripts/generate_tokens.py locally. "
            "Legacy GARMINTOKENS_BASE64 cannot be used by this version."
        )
    return {key: data[key] for key in ("di_token", "di_refresh_token", "di_client_id")}


class PersistentClient(Client):
    def _refresh_session(self):
        # Upstream 0.3.16 swallows refresh errors. Propagate instead so a 429
        # stops before any API request, and a failed disk write fails closed.
        with self._token_lock:
            self._refresh_di_token()
            if not self._tokenstore_path:
                raise StorageError("Token persistence is not configured")
            try:
                atomic_json(Path(self._tokenstore_path) / "garmin_tokens.json", validate_tokens(json.loads(self.dumps())))
            except OSError as exc:
                raise StorageError("Cannot persist refreshed tokens; requests stopped") from exc


def restore_client(store, transport_guard):
    client = Garmin(retry_attempts=0)
    client.client = PersistentClient()
    native = client.client
    native.load(str(store.directory))  # Local read only; never Garmin.login().
    native._api_session.request = transport_guard(native._api_session.request)
    native._http_post = transport_guard(native._http_post)
    # Load required profile fields once; upstream login retries these 3 times.
    profile = client.connectapi("/userprofile-service/socialProfile")
    settings = client.connectapi(client.garmin_connect_user_settings_url)
    if not isinstance(profile, dict) or not isinstance(settings, dict) or not isinstance(settings.get("userData"), dict):
        raise GarminSetupError("Garmin returned an invalid profile or settings response")
    client.display_name = profile.get("displayName")
    client.full_name = profile.get("fullName", "")
    client.profile_id = profile.get("profileId")
    client.unit_system = settings["userData"].get("measurementSystem")
    return client


class GarminProxy:
    def __init__(self, state_dir=None):
        self._client = None
        self._client_digest = None
        self._lock = threading.RLock()
        self._fatal_error = None
        self._last_error = None
        self._state = None
        self._store = None
        try:
            self._store = StateStore(state_dir)
        except (OSError, StorageError):
            self._fatal_error = "Persistent storage unavailable. Configure GARMIN_STATE_DIR on a persistent disk."
        try:
            cooldown = float(os.getenv("GARMIN_RATE_LIMIT_COOLDOWN_SEC", "1800"))
        except ValueError:
            cooldown = 1800.0
        self._cooldown = cooldown if math.isfinite(cooldown) and cooldown > 0 else 1800.0

    def _save(self):
        try:
            self._store.save_guard(self._state)
        except StorageError:
            self._fatal_error = "Cannot persist request guard; requests stopped"
            raise

    def _check_cooldown(self):
        if self._fatal_error:
            raise StorageError(self._fatal_error)
        remaining = self._state["blocked_until"] - time.time()
        if remaining > 0:
            raise GarminRateLimitError(remaining)

    def _record_rate_limit(self, exc):
        # A low-level transport can already have recorded this same error.
        # Some upstream wrappers discard the original exception/headers.
        remaining = self._state["blocked_until"] - time.time()
        if remaining > 0:
            return GarminRateLimitError(remaining)
        for current in _exceptions(exc):
            if isinstance(current, GarminRateLimitError):
                return current
        self._state["rate_limit_count"] += 1
        fallback = min(self._cooldown * (2 ** min(self._state["rate_limit_count"] - 1, 10)), 7200)
        delay = max(fallback, _retry_after(exc) or 0)
        self._state["blocked_until"] = time.time() + delay
        self._save()
        return GarminRateLimitError(delay)

    def _transport_guard(self, operation):
        @functools.wraps(operation)
        def guarded(*args, **kwargs):
            self._check_cooldown()
            response = operation(*args, **kwargs)
            if getattr(response, "status_code", None) == 429:
                exc = RuntimeError("Garmin HTTP 429")
                exc.response = response
                raise self._record_rate_limit(exc)
            return response
        return guarded

    def _prepare_tokens(self):
        if not self._store.tokens.exists():
            source = os.getenv("GARMIN_TOKEN_SOURCE")
            inline = os.getenv("GARMIN_TOKENS_JSON")
            try:
                if source:
                    tokens = read_json(Path(source).expanduser())
                elif inline:
                    tokens = json.loads(inline)
                else:
                    raise GarminSetupError(
                        "No modern token file. Run scripts/generate_tokens.py locally, "
                        "then provide GARMIN_TOKEN_SOURCE or GARMIN_TOKENS_JSON. "
                        "Legacy GARMINTOKENS_BASE64 is not supported. No login was attempted."
                    )
                atomic_json(self._store.tokens, validate_tokens(tokens))
            except (OSError, ValueError) as exc:
                raise GarminSetupError("Cannot import token file; no login was attempted") from exc
        try:
            tokens = validate_tokens(read_json(self._store.tokens))
        except (OSError, ValueError) as exc:
            raise GarminSetupError("Cannot read saved tokens; refusing stale bootstrap fallback") from exc
        digest = hashlib.sha256(json.dumps(tokens, sort_keys=True).encode()).hexdigest()
        if self._state.get("auth_required") and self._state.get("token_digest") == digest:
            raise GarminSetupError("Saved credentials were rejected. Replace the token file using a local interactive login; automatic login is stopped.")
        if self._state.get("token_digest") != digest:
            self._state["auth_required"] = False
            self._state["token_digest"] = digest
            self._save()
        if self._client_digest != digest:
            self._client = None
        return digest

    def _call(self, name, *args, **kwargs):
        if self._fatal_error:
            raise StorageError(self._fatal_error)
        with self._lock, self._store.locked():
            self._state = self._store.read_guard()
            self._check_cooldown()
            digest = self._prepare_tokens()
            try:
                if self._client is None:
                    self._client = restore_client(self._store, self._transport_guard)
                    self._client_digest = digest
                result = getattr(self._client, name)(*args, **kwargs)
            except Exception as exc:
                if any(isinstance(error, StorageError) for error in _exceptions(exc)):
                    self._fatal_error = "Cannot persist refreshed tokens; requests stopped"
                    raise StorageError(self._fatal_error) from exc
                if _is_rate_limit(exc):
                    error = self._record_rate_limit(exc)
                    self._last_error = str(error)
                    if error is exc:
                        raise
                    raise error from exc
                if _is_auth_error(exc):
                    self._state["auth_required"] = True
                    # Refresh may already have rotated the file before a 401.
                    tokens = validate_tokens(read_json(self._store.tokens))
                    self._state["token_digest"] = hashlib.sha256(json.dumps(tokens, sort_keys=True).encode()).hexdigest()
                    self._save()
                    self._client = None
                    raise GarminSetupError("Garmin rejected the saved credentials. Automatic login is stopped; regenerate tokens locally.") from exc
                raise
            # Persisted rotations take precedence over the bootstrap secret.
            tokens = validate_tokens(read_json(self._store.tokens))
            self._client_digest = hashlib.sha256(json.dumps(tokens, sort_keys=True).encode()).hexdigest()
            self._state.update(rate_limit_count=0, blocked_until=0.0, auth_required=False, token_digest=self._client_digest)
            self._save()
            self._last_error = None
            return result

    def status(self):
        # Atomic reads only: health checks never log in or wait on network locks.
        state = {}
        error = self._fatal_error
        if self._store:
            try:
                state = self._store.read_guard()
            except StorageError:
                error = "Cannot read persisted request guard"
        return {
            "authenticated": self._client is not None and not state.get("auth_required", False),
            "storage_ready": self._store is not None and error is None,
            "token_file_present": self._store is not None and self._store.tokens.is_file(),
            "auth_required": state.get("auth_required", False),
            "retry_after_seconds": max(0, math.ceil(state.get("blocked_until", 0) - time.time())),
            "last_error": error or self._last_error,
        }

    def __getattr__(self, name):
        if name.startswith("_") or name in ("client", "garth"):
            raise AttributeError(name)
        return functools.partial(self._call, name)


def init_garmin_client():
    return GarminProxy()
