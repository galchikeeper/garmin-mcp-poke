#!/usr/bin/env python3
"""Explicitly replace saved DI tokens without resetting a 429 deadline."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from garmin_client import GarminSetupError, validate_tokens
from state_store import StateStore, StorageError, atomic_json, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--state-dir")
    args = parser.parse_args()
    try:
        store = StateStore(args.state_dir)
        with store.locked():
            tokens = validate_tokens(read_json(args.source.expanduser()))
            state = store.read_guard()
            # Clear the auth-required flag only AFTER the new token is durable.
            atomic_json(store.tokens, tokens)
            state["auth_required"] = False
            state.pop("token_digest", None)
            store.save_guard(state)
    except (OSError, ValueError, StorageError, GarminSetupError):
        print("Import failed. Verify the source contains modern DI tokens and the state directory is writable. No credentials were printed.", file=sys.stderr)
        return 1
    print("Saved new tokens privately. Any active 429 cooldown is unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
