import base64
import copy
import json
import unittest

from cryptography.fernet import Fernet

from src.token_store import FILENAME, MAX_CONTENT_BYTES, REQUEST_TIMEOUT, TokenCodec, TokenStore, TokenStoreError


class TokenCodecTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key()
        self.codec = TokenCodec(self.key)
        self.state = {
            "version": 1,
            "tokens": base64.b64encode(b'{"access_token":"test-placeholder"}').decode(),
            "cooldown_until": 1234567890,
            "last_failure": "garmin_rate_limited",
        }

    def assert_code(self, code, callback):
        with self.assertRaises(TokenStoreError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_encrypted_roundtrip_and_envelope_has_no_plaintext_metadata(self):
        content = self.codec.encode(self.state)
        envelope = json.loads(content)
        self.assertEqual(set(envelope), {"version", "ciphertext"})
        self.assertEqual(envelope["version"], 1)
        self.assertNotIn("test-placeholder", content)
        self.assertNotIn(self.state["tokens"], content)
        self.assertNotIn("cooldown", content)
        self.assertEqual(self.codec.decode(content), self.state)
        self.assertNotEqual(self.codec.encode(self.state), content)

    def test_malformed_key_errors_never_echo_key(self):
        for key in ("test-private-key-placeholder", "é", None, 12, b"a" * 44):
            self.assert_code("invalid_encryption_key", lambda key=key: TokenCodec(key))

    def test_malformed_state_rejected(self):
        malformed = [
            [], {}, {"version": True}, {"version": 2},
            {"version": 1, "tokens": 12}, {"version": 1, "tokens": ""},
            {"version": 1, "tokens": "not base64"}, {"version": 1, "tokens": "é"},
            {"version": 1, "cooldown_until": float("nan")},
            {"version": 1, 2: "bad-key"}, {"version": 1, "metadata": set()},
        ]
        for state in malformed:
            self.assert_code("invalid_state", lambda state=state: self.codec.encode(state))
        circular = {"version": 1}
        circular["self"] = circular
        self.assert_code("invalid_state", lambda: self.codec.encode(circular))

    def test_malformed_ciphertext_and_wrong_key_are_rejected(self):
        for ciphertext in ("test-placeholder", "é", ""):
            content = json.dumps({"version": 1, "ciphertext": ciphertext})
            self.assert_code("decryption_failed", lambda: self.codec.decode(content))
        content = self.codec.encode(self.state)
        other = TokenCodec(Fernet.generate_key())
        self.assert_code("decryption_failed", lambda: other.decode(content))

    def test_tampered_ciphertext_is_rejected(self):
        envelope = json.loads(self.codec.encode(self.state))
        ciphertext = bytearray(base64.urlsafe_b64decode(envelope["ciphertext"]))
        ciphertext[len(ciphertext) // 2] ^= 1
        envelope["ciphertext"] = base64.urlsafe_b64encode(ciphertext).decode()
        self.assert_code("decryption_failed", lambda: self.codec.decode(json.dumps(envelope)))

    def test_bad_envelope_and_decrypted_state_fail_closed(self):
        for content in (
            "not-json", "[]", '{}', '{"version":1,"tokens":"plaintext"}',
            '{"version":true,"ciphertext":"bad"}',
            '{"version":1,"version":1,"ciphertext":"bad"}', None,
        ):
            self.assert_code("invalid_envelope", lambda content=content: self.codec.decode(content))
        for plaintext in (b"[]", b'{"version":1,"tokens":"bad"}', b'{"version":1,"value":NaN}'):
            content = json.dumps({"version": 1,
                                 "ciphertext": Fernet(self.key).encrypt(plaintext).decode()})
            self.assert_code("invalid_state", lambda: self.codec.decode(content))

    def test_content_size_is_bounded(self):
        state = {"version": 1, "metadata": "x" * MAX_CONTENT_BYTES}
        self.assert_code("state_too_large", lambda: self.codec.encode(state))
        self.assert_code("invalid_envelope", lambda: self.codec.decode("x" * (MAX_CONTENT_BYTES + 1)))

    def test_metadata_only_cooldown_state_is_supported(self):
        state = {"version": 1, "tokens": None, "cooldown_until": 1234567890}
        self.assertEqual(self.codec.decode(self.codec.encode(state)), state)

    def test_runtime_metadata_schema_prevents_invalid_cooldown_and_dates(self):
        invalid = [
            {"failure_count": True}, {"failure_count": -1}, {"failure_count": "2"},
            {"login_cooldown_until": None}, {"login_cooldown_until": "tomorrow"},
            {"login_cooldown_until": float("nan")}, {"login_cooldown_until": -1},
            {"last_login_at": float("inf")}, {"last_success_at": True},
            {"last_refresh_at": 253402300800}, {"last_rate_limit_at": "bad"},
            {"last_error": "exception containing a token"},
        ]
        for fields in invalid:
            state = {"version": 1, **fields}
            self.assert_code("invalid_state", lambda: self.codec.encode(state))
            content = json.dumps({"version": 1,
                "ciphertext": Fernet(self.key).encrypt(json.dumps(state).encode()).decode()})
            self.assert_code("invalid_state", lambda: self.codec.decode(content))


class Response:
    def __init__(self, body=None, status=200):
        self.body, self.status_code = body, status

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return copy.deepcopy(self.body)


class MemoryGithub:
    def __init__(self):
        self.gist = {"public": False, "files": {
            FILENAME: {"filename": FILENAME, "content": "", "truncated": False}}}
        self.calls = []
        self.read_error = self.write_error = None
        self.read_status = self.write_status = 200

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if self.read_error:
            raise self.read_error
        return Response(self.gist, self.read_status)

    def patch(self, url, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        if self.write_error:
            raise self.write_error
        if self.write_status == 200:
            self.gist["files"][FILENAME]["content"] = kwargs["json"]["files"][FILENAME]["content"]
        return Response(self.gist, self.write_status)


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key()
        self.github = MemoryGithub()
        self.store = TokenStore("0123456789abcdef", "github-test-placeholder", self.key, self.github)
        self.state = {"version": 1, "tokens": base64.b64encode(b"test-placeholder").decode(),
                      "failure_count": 1, "login_cooldown_until": 1234567890,
                      "last_error": "GARMIN_RATE_LIMITED"}

    def assert_code(self, code, callback):
        with self.assertRaises(TokenStoreError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_store_roundtrip_encrypted_and_requests_bounded(self):
        self.assertEqual(self.github.calls, [])
        self.store.save(self.state)
        content = self.github.gist["files"][FILENAME]["content"]
        self.assertEqual(set(json.loads(content)), {"version", "ciphertext"})
        self.assertNotIn(self.state["tokens"], content)
        self.assertNotIn("login_cooldown", content)
        self.assertEqual(self.store.load(), self.state)
        self.assertEqual([call[0] for call in self.github.calls], ["GET", "PATCH", "GET"])
        for _method, url, kwargs in self.github.calls:
            self.assertEqual(url, "https://api.github.com/gists/0123456789abcdef")
            self.assertEqual(kwargs["timeout"], REQUEST_TIMEOUT)
            self.assertFalse(kwargs["allow_redirects"])

    def test_public_or_unknown_visibility_rejected_before_read_or_write(self):
        for visibility in (True, None, 0, "false"):
            self.github.gist["public"] = visibility
            self.assert_code("gist_not_secret", self.store.load)
            self.assert_code("gist_not_secret", lambda: self.store.save(self.state))
        self.assertFalse(any(call[0] == "PATCH" for call in self.github.calls))

    def test_missing_or_renamed_file_is_rejected(self):
        for files in ({}, {FILENAME: {"filename": "other.json"}}):
            self.github.gist["files"] = files
            self.assert_code("token_file_missing", self.store.load)
            self.assert_code("token_file_missing", lambda: self.store.save(self.state))

    def test_truncated_file_does_not_follow_raw_url(self):
        self.github.gist["files"][FILENAME].update(truncated=True, raw_url="https://hostile.example/secret")
        self.assert_code("token_file_truncated", self.store.load)
        self.assert_code("token_file_truncated", lambda: self.store.save(self.state))
        self.assertEqual(len(self.github.calls), 2)
        self.assertTrue(all(call[0] == "GET" for call in self.github.calls))

    def test_storage_failures_use_fixed_codes(self):
        raw = "test-placeholder github-test-placeholder private-key-placeholder"
        self.github.read_error = RuntimeError(raw)
        self.assert_code("github_read_failed", self.store.load)
        self.github.read_error = None
        self.github.write_error = RuntimeError(raw)
        self.assert_code("github_write_failed", lambda: self.store.save(self.state))
        self.github.write_error = None
        for status in (301, 401, 403, 429, 500):
            self.github.read_status = status
            self.assert_code("github_read_failed", self.store.load)
        self.github.read_status = 200
        for status in (301, 401, 403, 429, 500):
            self.github.write_status = status
            self.assert_code("github_write_failed", lambda: self.store.save(self.state))

    def test_bad_configuration_is_rejected_before_requests(self):
        self.assert_code("invalid_gist_id", lambda: TokenStore("../elsewhere", "token", self.key))
        self.assert_code("invalid_github_token", lambda: TokenStore("abc123", "token\nother", self.key))
        self.assertEqual(self.github.calls, [])

    def test_bad_github_response_is_sanitized(self):
        for body in (None, [], RuntimeError("test-placeholder")):
            self.github.gist = body
            self.assert_code("invalid_gist_response", self.store.load)


if __name__ == "__main__":
    unittest.main()
