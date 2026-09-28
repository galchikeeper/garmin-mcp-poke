#!/usr/bin/env python3
"""One explicit LOCAL password login after persisted cooldown, never a loop.

The encrypted store must be provisioned first. Values are written only to a
new mode-0600 seed file, not stdout. Server processes never use credentials.
"""
import argparse
import getpass
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dotenv import load_dotenv
load_dotenv()
from garminconnect import Garmin
from garmin_client import BACKOFF, error_details
from token_store import TokenStore, TokenStoreError
from safe_logging import suppress_upstream_logs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-cooldown-elapsed", action="store_true",
                        help="Explicitly authorize one local login; does not bypass saved cooldown.")
    parser.add_argument("--output", default=".secrets/garmin-seed.env",
                        help="New 0600 file for the read-only Render seed; never overwrite.")
    args = parser.parse_args()
    if not args.confirm_cooldown_elapsed:
        parser.error("Review cooldown first, then use --confirm-cooldown-elapsed.")
    if os.getenv("RENDER"):
        parser.error("Run this on your local computer, never on Render.")
    suppress_upstream_logs()
    output = Path(args.output)
    if output.exists():
        parser.error("Output already exists. Select a new protected file.")
    try:
        store = TokenStore(os.getenv("GIST_ID", ""), os.getenv("GITHUB_TOKEN", ""),
                           os.getenv("TOKEN_ENCRYPTION_KEY", ""))
        state = store.load()
    except TokenStoreError:
        parser.exit(1, "Encrypted token store is unavailable; no Garmin request made.\n")
    now = time.time()
    if now < state.get("login_cooldown_until", 0):
        parser.exit(1, "Persisted cooldown is active; no Garmin request made.\n")
    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")
    if not email or not password:
        parser.exit(1, "Email and password are required.\n")

    # Save before touching Garmin. A killed local login retains a safe delay.
    state["login_cooldown_until"] = now + 6 * 60 * 60
    try:
        store.save(state)
    except TokenStoreError:
        parser.exit(1, "Cannot persist the attempt guard; no Garmin request made.\n")
    garmin = Garmin()
    garmin.garth.configure(retries=0, timeout=20)
    try:
        # Garth acquires/stores both tokens without Garmin.login()'s extra
        # profile/settings reads. A failing profile read must not lose a valid
        # new token and cause a second password login.
        garmin.garth.login(email, password,
                           prompt_mfa=lambda: getpass.getpass("Garmin MFA code: ").strip())
    except Exception as exc:
        status, retry_after = error_details(exc, time.time())
        count = state.get("failure_count", 0) + 1
        delay = max(BACKOFF[min(count - 1, 3)], retry_after, 6 * 60 * 60)
        state.update(failure_count=count, login_cooldown_until=time.time() + delay,
                     last_error="GARMIN_RATE_LIMITED" if status == 429 else "LOCAL_LOGIN_FAILED")
        if status == 429:
            state["last_rate_limit_at"] = time.time()
        try:
            store.save(state)
        except TokenStoreError:
            pass  # durable pre-attempt 6h guard is still present
        parser.exit(1, "Login failed; cooldown retained. No retry and no raw error printed.\n")

    tokens = garmin.garth.dumps()
    now = time.time()
    state.update(tokens=tokens, version=1, last_login_at=now,
                 failure_count=0, login_cooldown_until=0, last_error=None)
    # Attempt BOTH independent recovery destinations. A local filesystem error
    # must not skip the remote save after a successful credential login.
    remote_saved = True
    try:
        store.save(state)
    except TokenStoreError:
        remote_saved = False
    local_saved = True
    try:
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as target:
            target.write("GARMIN_TOKENS_BASE64=" + tokens + "\n")
            target.flush()
            os.fsync(target.fileno())
    except OSError:
        local_saved = False
    if not remote_saved and not local_saved:
        parser.exit(1, "Login succeeded but both recovery saves failed. Stop and investigate storage; do not retry login.\n")
    if not remote_saved:
        parser.exit(1, "Login succeeded; protected recovery seed saved, Gist save failed. Do not login again.\n")
    if not local_saved:
        parser.exit(1, "Login succeeded and encrypted Gist is saved; local backup failed. Do not login again.\n")
    print("One local login succeeded. Encrypted state saved; protected Render seed file created.")
    print("No tokens or personal profile data were printed.")


if __name__ == "__main__":
    main()
