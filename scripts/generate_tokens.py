#!/usr/bin/env python3
"""Interactive local login; save DI tokens privately without printing secrets."""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from garminconnect import Garmin
from garmin_client import GarminProxy, GarminRateLimitError, PersistentClient, validate_tokens
from state_store import atomic_json


class InteractiveClient(PersistentClient):
    def _mobile_login_requests(self, email, password):
        # The upstream method creates a new Session; guard that session too,
        # including subsequent MFA requests made on it.
        session = requests.Session()
        session.request = self.transport_guard(session.request)
        self._do_mobile_login(session, email, password)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="~/.garmin-mcp")
    args = parser.parse_args()
    proxy = GarminProxy(args.state_dir)
    if proxy._fatal_error:
        parser.error(proxy._fatal_error)
    store = proxy._store
    with proxy._lock, store.locked():
        proxy._state = store.read_guard()
        try:
            proxy._check_cooldown()
        except GarminRateLimitError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        email = input("Garmin email: ").strip()
        password = getpass.getpass("Garmin password (hidden): ")
        if not email or not password:
            print("Email and password are required.", file=sys.stderr)
            return 1
        client = Garmin(email=email, password=password, retry_attempts=0,
                        prompt_mfa=lambda: input("Garmin MFA code: ").strip())
        del password
        client.client = InteractiveClient()
        client.client.transport_guard = proxy._transport_guard
        # Exactly one login strategy: no switch to another after an error/429.
        client.client.skip_strategies = {"mobile+cffi", "widget+cffi", "portal+cffi", "portal+requests"}
        client.client.cs.request = proxy._transport_guard(client.client.cs.request)
        client.client._api_session.request = proxy._transport_guard(client.client._api_session.request)
        client.client._http_post = proxy._transport_guard(client.client._http_post)
        old_tokenstore = os.environ.pop("GARMINTOKENS", None)
        try:
            # No tokenstore argument: a user-requested login creates new tokens,
            # while the existing file stays untouched until login succeeds.
            client.login()
            proxy._check_cooldown()  # Upstream may have caught a guarded 429.
            tokens = validate_tokens(json.loads(client.client.dumps()))
            atomic_json(store.tokens, tokens)
            proxy._state.update(auth_required=False, blocked_until=0.0, rate_limit_count=0)
            proxy._save()
        except Exception as exc:
            from garmin_client import _is_rate_limit
            if _is_rate_limit(exc):
                print(str(proxy._record_rate_limit(exc)), file=sys.stderr)
            else:
                print("Login did not complete. No token was printed or replaced. Check your credentials/MFA and avoid repeated attempts.", file=sys.stderr)
            return 1
        finally:
            client.password = None
            if old_tokenstore is not None:
                os.environ["GARMINTOKENS"] = old_tokenstore
        print(f"Saved private tokens: {store.tokens}")
        print("Do not paste the token contents into a chat or commit them to Git.")
        print("For Render, add the contents as Secret File garmin_tokens.json.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
