#!/usr/bin/env python3
"""Create one EMPTY encrypted secret Gist. Never contacts Garmin.

Uses existing gh authentication only to provision the Gist; never exports its
token to Render. Set a dedicated gist-only token there separately.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from token_store import FILENAME, TokenCodec


def gh_api(arguments, payload=None):
    result = subprocess.run(["gh", "api", *arguments],
                            input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, timeout=45)
    if result.returncode:
        raise RuntimeError("GitHub request failed; response omitted.")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".secrets/store.env")
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        parser.error("Protected configuration already exists; it will not be replaced.")
    # Refuse duplicate stores. No token contents are read or logged.
    pages = gh_api(["--paginate", "--slurp", "gists?per_page=100"])
    if any(FILENAME in item.get("files", {}) for page in pages for item in page):
        parser.error("A Garmin token Gist already exists. Reuse it with its original key.")

    key = Fernet.generate_key().decode("ascii")
    state = {"version": 1, "tokens": None, "failure_count": 0,
             "last_login_at": None, "last_success_at": None,
             "last_error": "LOCAL_TOKEN_SEED_REQUIRED",
             "login_cooldown_until": time.time() + 6 * 60 * 60}
    content = TokenCodec(key).encode(state)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Retain key even if the remote create succeeds but its response is lost.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as target:
        target.write("TOKEN_ENCRYPTION_KEY=" + key + "\n")
        target.flush()
        os.fsync(target.fileno())
        result = gh_api(["--method", "POST", "gists", "--input", "-"], {
            "description": "Garmin MCP encrypted state v1", "public": False,
            "files": {FILENAME: {"content": content}},
        })
        if result.get("public") is not False or not result.get("id"):
            raise RuntimeError("Gist privacy could not be verified; key retained locally.")
        target.write("GIST_ID=" + result["id"] + "\n")
    print("Created empty encrypted secret Gist. Key and ID saved in the protected output file.")
    print("No Garmin login or token exchange occurred. Set a gist-only GitHub token separately.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired, ValueError, OSError):
        raise SystemExit("Provisioning did not finish. Protected key retained if created; do not retry blindly.") from None
