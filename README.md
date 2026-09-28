# Garmin MCP Server for Poke

A [Poke](https://poke.com)-compatible MCP server exposing 90+ [Garmin Connect](https://connect.garmin.com) tools over HTTP via [FastMCP](https://github.com/jlowin/fastmcp). Deployable to Render.

Built on top of [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) and [InteractionCo/mcp-server-template](https://github.com/InteractionCo/mcp-server-template).

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/theol0403/garmin-mcp-poke)

## Setup

### 1. Generate OAuth Tokens

Garmin accounts with MFA require local token generation:

```bash
pip install garminconnect garth
python scripts/generate_tokens.py
```

This will prompt for your Garmin email, password, and MFA code, then output a base64 token string.

### 2. Deploy to Render

#### Option 1: One-Click Deploy
Click the "Deploy to Render" button above, then set `GARMINTOKENS_BASE64` to the token string from step 1.

#### Option 2: Manual Deployment
1. Fork this repository
2. Connect your GitHub account to Render
3. Create a new Web Service on Render
4. Connect your forked repository
5. Render will automatically detect the `render.yaml` configuration
6. Set `GARMINTOKENS_BASE64` in environment variables

Your server will be available at `https://your-service-name.onrender.com/mcp`

### 3. Connect Poke

Add your Render URL to Poke at [poke.com/settings/connections](https://poke.com/settings/connections):
```
https://your-service-name.onrender.com/mcp
```

## Local Development

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set GARMINTOKENS_BASE64
python src/server.py
```

Test with MCP Inspector:
```bash
npx @modelcontextprotocol/inspector
```
Open http://localhost:3000 and connect to `http://localhost:8000/mcp` using "Streamable HTTP" transport.

## Token Refresh

### Garmin authentication returns HTTP 429

The server pauses Garmin requests for 30 minutes after a 429, doubles that
local delay on repeated failures (up to two hours), and respects longer
`Retry-After` values when the upstream exception exposes that header. This
is a client cooldown, **not a guarantee that Garmin will accept the next request**.
`GARMIN_RATE_LIMIT_COOLDOWN_SEC` changes the initial local delay in seconds.
Requests and token refreshes are serialized within the server process.
`/health` reports `retry_after_seconds` without contacting Garmin.

Authentication warmup is disabled by default. Render startup and health checks
do not initiate a Garmin login. Set `GARMIN_AUTH_WARMUP=true` only if startup
authentication is intentionally required.

Do not repeatedly restart, redeploy, regenerate tokens, or retry from multiple
clients during a rate limit. The cooldown is held in memory and resets when
the process restarts; it is not coordinated across multiple server replicas.
After the cooldown, make one small read to check access. If it returns 429,
stop and investigate authentication library compatibility and token validity.

This guard does not repair expired tokens or migrate the legacy Garth login
flow. The server still intentionally pins garminconnect below 0.3 because
its existing token and workout code depend on Garth. Changing that pin alone
is not a supported migration.

Run the credential-free regression tests with:

```bash
python -m unittest discover -s tests -v
```

Tokens last approximately 6 months. When they expire, re-run `generate_tokens.py` and update the Render env var.

## Tool Categories

| Category | Tools | Examples |
|----------|-------|---------|
| Activity Management | 14 | Activities by date, splits, weather, HR zones, gear |
| Health & Wellness | 28 | Stats, sleep, stress, body battery, HRV, SpO2, steps |
| Training | 10 | Training status, endurance score, hill score, lactate threshold |
| User Profile | 4 | Profile info, settings, unit system |
| Devices | 7 | Device list, settings, alarms, solar data |
| Gear Management | 3 | Gear inventory, add/remove gear from activities |
| Weight Management | 5 | Weigh-ins, add/delete measurements |
| Challenges | 10 | Goals, badges, challenges, race predictions, PRs |
| Workouts | 7 | Workout library, scheduling, training plans, upload |
| Data Management | 3 | Body composition, blood pressure, hydration |
| Women's Health | 3 | Pregnancy, menstrual cycle tracking |
| Workout Templates | 5 | Resources with workout JSON templates |

## Attribution

Built on top of:
- [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) -- Garmin Connect MCP tool modules and authentication flow
- [InteractionCo/mcp-server-template](https://github.com/InteractionCo/mcp-server-template) -- FastMCP HTTP server template for Render/Poke
