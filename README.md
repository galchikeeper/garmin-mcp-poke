# Garmin MCP Server for Poke / Claude

A FastMCP HTTP server exposing Garmin Connect tools. Based on
[Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) and
[InteractionCo/mcp-server-template](https://github.com/InteractionCo/mcp-server-template).

## Migration requirements

**Python 3.12, modern DI tokens, and persistent storage are required.**
This version replaces the deprecated Garth authentication flow. Existing
`GARMINTOKENS_BASE64` tokens cannot be converted by changing a setting.
Server-side email/password login is disabled; generate tokens interactively.

**Do not deploy over the old version until these prerequisites are ready.**
The default Render Free plan cannot provide a persistent disk. The supplied
Blueprint does not purchase or upgrade a plan; the server deliberately refuses
Garmin requests on Render without a mounted persistent state directory.
See the [Korean deployment and rollback guide](docs/DEPLOYMENT_KO.md).

## Local setup

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/generate_tokens.py
HOST=127.0.0.1 .venv/bin/python src/server.py
```

The generator prompts for email, a hidden password, and MFA if required. It
writes `~/.garmin-mcp/garmin_tokens.json` privately; it never prints tokens.
Connect your MCP client to `http://127.0.0.1:8000/mcp` (Streamable HTTP).

## Durable authentication and rate limiting

- `GARMIN_STATE_DIR`: private directory holding current tokens and the request
  guard. Default locally: `~/.garmin-mcp`. Required on a mounted disk on Render.
- `GARMIN_TOKEN_SOURCE`: optional path to a modern JSON token seed, for example
  `/etc/secrets/garmin_tokens.json`. `GARMIN_TOKENS_JSON` is an inline alternative.
  Seeds are imported only if no saved token file exists.
- Expiring tokens refresh via DI and are atomically persisted before API traffic
  continues. Disk tokens always take precedence over old bootstrap secrets.
- A 429 persists a UTC deadline and failure count. Local delay starts at 30 min,
  doubles to 60 then 120 min, and honors longer upstream `Retry-After` headers.
  `GARMIN_RATE_LIMIT_COOLDOWN_SEC` configures the initial delay. This is our
  cooldown policy, **not a guarantee of Garmin's reset time**.
- Requests and refreshes share an in-process lock and a filesystem lock. All
  workers on the same disk observe the guard; independent replicas do not.
- Rejected credentials stop unattended retries until the saved token file is
  replaced. `scripts/import_tokens.py SOURCE` safely replaces them while
  preserving active rate-limit deadlines.
- Startup, tool discovery and `/health` make no Garmin requests. HTTP health
  is liveness only, not evidence of authentication success.
- Existing tool error strings become MCP `isError` responses.

The small native transport/refresh adapter uses private hooks of the exact
pinned garminconnect version. Do not update the dependency without rerunning
the offline integration tests. These hooks prevent upstream retry behavior
from swallowing refresh errors or sending another request after 429.

## Deployment

Read [DEPLOYMENT_KO.md](docs/DEPLOYMENT_KO.md) for the existing Render service.
Prepare the disk and new tokens first, then deploy manually. No keep-alive ping
job is required for correctness. Shared IP limits and Garmin-side outages are
outside this patch's control. A successful real activity read remains required
before claiming recovery.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src scripts tests
```

Tests mock HTTP using the real pinned Garmin/FastMCP dependencies. They cover
restart persistence, token rotation, 429 during refresh and profile reads,
private file permissions, fail-closed storage errors, request serialization,
workout API compatibility and actual MCP error propagation. They do not log in
to Garmin or perform live writes.

Existing health, training, activity, device, workout, gear, weight, challenge,
profile and women's health tool names are retained. Completed-activity FIT
bulk download is not added by this authentication repair.
