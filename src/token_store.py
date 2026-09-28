"""Encrypted token persistence in an existing GitHub secret Gist.

Secret Gists are unlisted rather than private. Only encrypted state is sent to
GitHub; the Fernet key is configured separately and never written to the Gist.
"""
import base64
import json
import math
import re

import requests
from cryptography.fernet import Fernet, InvalidToken


FILENAME = "garmin_tokens.json"
MAX_CONTENT_BYTES = 512 * 1024
REQUEST_TIMEOUT = (5, 15)
SAFE_ERROR_CODES = frozenset({
    "LOCAL_TOKEN_SEED_REQUIRED", "INVALID_TOKEN_SEED", "GARMIN_RATE_LIMITED",
    "GARMIN_AUTH_REJECTED_LOCAL_RESEED_REQUIRED", "GARMIN_REQUEST_FAILED", "LOCAL_LOGIN_FAILED",
})


class TokenStoreError(RuntimeError):
    """Fixed, sanitized error codes suitable for application diagnostics."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _valid_json_value(value, depth=0):
    if depth > 20:
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_valid_json_value(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _valid_json_value(item, depth + 1)
                   for key, item in value.items())
    return False


def _validate_state(state):
    if (not isinstance(state, dict) or type(state.get("version")) is not int
            or state["version"] != 1 or not _valid_json_value(state)):
        raise TokenStoreError("invalid_state")
    tokens = state.get("tokens")
    if tokens is not None:
        if not isinstance(tokens, str) or not tokens:
            raise TokenStoreError("invalid_state")
        try:
            decoded = base64.b64decode(tokens.encode("ascii"), validate=True)
        except (ValueError, UnicodeError):
            raise TokenStoreError("invalid_state") from None
        if not decoded:
            raise TokenStoreError("invalid_state")
    if "failure_count" in state and (type(state["failure_count"]) is not int
                                     or state["failure_count"] < 0):
        raise TokenStoreError("invalid_state")
    for field in ("login_cooldown_until", "last_login_at", "last_success_at",
                  "last_refresh_at", "last_rate_limit_at"):
        value = state.get(field)
        if value is None and field != "login_cooldown_until":
            continue
        if field not in state:
            continue
        if type(value) not in (int, float) or not 0 <= value <= 253402300799:
            raise TokenStoreError("invalid_state")
    error = state.get("last_error")
    if error is not None and (not isinstance(error, str) or error not in SAFE_ERROR_CODES):
        raise TokenStoreError("invalid_state")


def _strict_json(content):
    def reject_constant(_value):
        raise ValueError()

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    return json.loads(content, parse_constant=reject_constant,
                      object_pairs_hook=reject_duplicates)


class TokenCodec:
    """Encode/decode in-memory state; never perform any storage or networking.

    State is a JSON object with integer version 1, optional base64 ``tokens``
    (or null), and JSON metadata. The outer envelope contains only version and
    authenticated ciphertext. Each encryption uses a fresh random IV.
    """

    def __init__(self, encryption_key):
        try:
            key = encryption_key.encode("ascii") if isinstance(encryption_key, str) else encryption_key
            if (not isinstance(key, bytes)
                    or len(base64.b64decode(key, altchars=b"-_", validate=True)) != 32):
                raise ValueError()
            self._cipher = Fernet(key)
        except (ValueError, TypeError, UnicodeError):
            raise TokenStoreError("invalid_encryption_key") from None

    def encode(self, state):
        _validate_state(state)
        try:
            plaintext = json.dumps(state, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise TokenStoreError("invalid_state") from None
        if len(plaintext) > MAX_CONTENT_BYTES // 2:
            raise TokenStoreError("state_too_large")
        return json.dumps(
            {"version": 1, "ciphertext": self._cipher.encrypt(plaintext).decode("ascii")},
            separators=(",", ":"))

    def decode(self, content):
        try:
            valid_content = isinstance(content, str) and len(content.encode("utf-8")) <= MAX_CONTENT_BYTES
        except UnicodeError:
            valid_content = False
        if not valid_content:
            raise TokenStoreError("invalid_envelope")
        try:
            envelope = _strict_json(content)
        except (ValueError, TypeError, RecursionError):
            raise TokenStoreError("invalid_envelope") from None
        if (not isinstance(envelope, dict) or set(envelope) != {"version", "ciphertext"}
                or type(envelope.get("version")) is not int or envelope["version"] != 1
                or not isinstance(envelope.get("ciphertext"), str)):
            raise TokenStoreError("invalid_envelope")
        try:
            plaintext = self._cipher.decrypt(envelope["ciphertext"].encode("ascii"))
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            raise TokenStoreError("decryption_failed") from None
        try:
            state = _strict_json(plaintext)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise TokenStoreError("invalid_state") from None
        _validate_state(state)
        return state


class TokenStore:
    """GET/PATCH one encrypted file, with visibility checked before every write.

    Missing/malformed storage is an error, never a reason to retry Garmin login.
    No network calls occur at construction. A requests-compatible session can
    be injected for deterministic tests. The raw_url is never used.
    """

    def __init__(self, gist_id, github_token, encryption_key, session=None):
        if not isinstance(gist_id, str) or not re.fullmatch(r"[a-fA-F0-9]{1,64}", gist_id):
            raise TokenStoreError("invalid_gist_id")
        if (not isinstance(github_token, str) or not github_token.strip()
                or "\r" in github_token or "\n" in github_token):
            raise TokenStoreError("invalid_github_token")
        self._codec = TokenCodec(encryption_key)
        self._url = f"https://api.github.com/gists/{gist_id}"
        self._headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._session = session if session is not None else requests.Session()

    def _read_file(self):
        try:
            response = self._session.get(self._url, headers=self._headers,
                                         timeout=REQUEST_TIMEOUT, allow_redirects=False)
        except Exception:
            raise TokenStoreError("github_read_failed") from None
        if getattr(response, "status_code", None) != 200:
            raise TokenStoreError("github_read_failed")
        try:
            gist = response.json()
        except Exception:
            raise TokenStoreError("invalid_gist_response") from None
        if not isinstance(gist, dict):
            raise TokenStoreError("invalid_gist_response")
        if gist.get("public") is not False:
            raise TokenStoreError("gist_not_secret")
        files = gist.get("files")
        file = files.get(FILENAME) if isinstance(files, dict) else None
        if not isinstance(file, dict) or file.get("filename") != FILENAME:
            raise TokenStoreError("token_file_missing")
        if file.get("truncated") is not False and file.get("truncated") is not None:
            raise TokenStoreError("token_file_truncated")
        return file

    def load(self):
        return self._codec.decode(self._read_file().get("content"))

    def save(self, state):
        content = self._codec.encode(state)
        self._read_file()
        try:
            response = self._session.patch(self._url, headers=self._headers,
                                           json={"files": {FILENAME: {"content": content}}},
                                           timeout=REQUEST_TIMEOUT, allow_redirects=False)
        except Exception:
            raise TokenStoreError("github_write_failed") from None
        if getattr(response, "status_code", None) != 200:
            raise TokenStoreError("github_write_failed")
