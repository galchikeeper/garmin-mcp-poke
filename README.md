# Garmin MCP Server

The existing Garmin tools remain available over Streamable HTTP at `/mcp`.
Authentication state now survives Render free-instance restarts through encrypted
GitHub Gist persistence. Process startup and `GET /health` never call Garmin.

## Recovery setup

1. Install the tested dependency family:
   `python -m pip install -r requirements-dev.txt`.
   This pins **garminconnect 0.2.40 / garth 0.5.21**. The wrapper depends on their
   actual `Garmin.login(tokenstore)`, `garth.loads/dumps`, and `refresh_oauth2`
   APIs. Do not upgrade to garminconnect 0.3 without adapting authentication.
2. Provision an encrypted empty state with
   `python scripts/provision_store.py`, using an already authenticated GitHub CLI.
   It refuses duplicate Garmin stores. It creates a **secret** Gist and a
   mode-0600 `.secrets/store.env` containing its ID and a separate Fernet key.
   No Garmin request occurs. The initial state contains a six-hour recovery hold.
3. Configure Render with `GIST_ID`, `TOKEN_ENCRYPTION_KEY`, and a dedicated
   **gist-only** GitHub PAT in `GITHUB_TOKEN`. Do not export a general GitHub CLI
   credential to Render. Preserve the encryption key in your private secret
   manager. Losing it makes the Gist history unreadable.
4. Restore an existing known-good `garth.dumps()` seed via
   `GARMIN_TOKENS_BASE64` (legacy `GARMINTOKENS_BASE64` is accepted).
   The seed is read-only; refreshed values are persisted to Gist.
   If a new seed really is necessary, set local environment values from step 3
   and run `python scripts/generate_tokens.py --confirm-cooldown-elapsed` **once**
   on your own computer after the incident cooldown. The script checks persisted
   cooldown, uses hidden password/MFA entry, performs one Garth login, and saves
   tokens to Gist plus a mode-0600 seed file. It never prints token values.
   A failed login exits without retry. Do not run it on Render.
5. Deploy the reviewed code with the environment in place and explicit approval.
   Verify the live Dashboard setting rather than trusting `render.yaml` alone.
   The authorized rollout on 2026-09-28 set service Auto-Deploy to **Off** and
   paused the legacy upstream Blueprint's Auto Sync to prevent configuration drift.
   Run a single read-only Garmin tool after cooldown;
   then confirm `authenticated: true`, `token_source: gist`, and no storage error.
6. Follow [ops/OPERATIONS.md](ops/OPERATIONS.md) before enabling keepalive.
   The template in `ops/keepalive.yml` is inactive; the installed workflow is
   `.github/workflows/garmin-keepalive.yml`. Its gate was enabled after checking
   the free-instance-hour budget and live safe-health/restart behavior.

A secret Gist is unlisted, **not private**. Only authenticated ciphertext is
stored there; the key, Garmin password, and GitHub credential never go into the
Gist or Git. Base64 alone is not encryption. The server verifies Gist visibility,
rejects truncated/malformed data, uses bounded timeouts, and does not follow
redirects with the GitHub credential.

## Runtime behavior

- Restore Gist state first. If a readable store has no tokens, restore the
  environment seed offline and save it with existing cooldown metadata intact.
- If Gist cannot be read/decrypted, fail closed. An old environment seed must
  not erase a cooldown that could not be retrieved. No credential fallback.
- Startup restores storage state only. The first actual tool call lazily loads
  Garmin profile/settings from the saved tokens. It never has a password.
- Serialize tool calls and OAuth refresh with a lock. Persist a recovery delay
  before each Garmin request (including already-authenticated calls), then
  persist refreshed tokens immediately. A storage outage blocks new calls.
- Back off 15 minutes, 1 hour, 6 hours, then 24 hours on consecutive failures.
  HTTP 429 waits at least six hours and honors a longer `Retry-After`.
  Preserve the failure streak until the requested operation succeeds.
- Save cooldown across restarts. Health polling neither clears it nor retries
  authentication. A disappearing error does not prove Garmin lifted a limit.
- Never automatically replay a failed tool; some existing tools write data.
  A lost response must not duplicate a write.
- Execute blocking Garmin handlers on worker threads so HTTP health remains
  responsive. Existing tool names, parameter schemas, and results are retained.
- Suppress upstream OAuth/header and health-payload logs, including child
  loggers and Garth's direct root warnings, while retaining application logs.

This deployment is designed for **one Render instance/process**. The lock is
process-local; Gist is not an atomic distributed-lock service. Do not run multiple
workers/replicas or overlap a local reseed with server tool calls. Quiesce existing
consumer schedules during reseeding/redeployment and resume after the old process
has exited. Cross-process exactly-once authentication is not claimed.

## Health

Example shape after a successful tool call (illustrative, not live evidence):

```json
{
  "ok": true,
  "authenticated": true,
  "tokens_loaded": true,
  "token_source": "gist",
  "oauth1_expires_at": null,
  "oauth2_expires_at": "2026-09-28T08:00:00Z",
  "last_login_at": "2026-09-28T07:00:00Z",
  "last_success_at": "2026-09-28T07:01:00Z",
  "last_refresh_at": null,
  "last_rate_limit_at": null,
  "login_cooldown_until": null,
  "failure_count": 0,
  "uptime_seconds": 60,
  "persistence_error": null,
  "last_error": null,
  "server_password_login_enabled": false
}
```

`authenticated` means profile validation succeeded in this process; it is
initially false after offline restoration. `tokens_loaded` distinguishes that
normal state from missing credentials. `last_success_at` records actual successful
tool execution. `last_login_at` changes only on a successful **local password**
seed login, never on a server restart or OAuth2 refresh.

The pinned OAuth1 object has **no token expiry field**. Therefore
`oauth1_expires_at` is null, not an invented one-year date; MFA expiration is a
different field and must not be substituted. A token can be revoked earlier.

## Validation

```sh
python -m pytest -q
python -m compileall -q src scripts
python -m pip check
```

Tests use fake Garmin clients, fake HTTP storage and clocks. They cover restart
recovery, concurrent access, encrypted persistence, rate limits, response redaction,
health behavior and pinned offline token loading. They never authenticate to Garmin.

Live acceptance still requires Render environment access, a usable token seed,
one controlled restart with unchanged `last_login_at`, one read-only Garmin
request, and 30 minutes of monitor history. Do not remove consumers' cold-start
recovery instructions before those checks. Free hosting and GitHub cron cannot
guarantee uninterrupted or sub-second service.

## Existing tool categories

Activity, health/wellness, training, profile, devices, gear, weight, challenges,
workouts, body composition, hydration and women's health are preserved. Existing
tool access controls are unchanged; protect access to the MCP endpoint.

Built on [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) and
[InteractionCo/mcp-server-template](https://github.com/InteractionCo/mcp-server-template).
