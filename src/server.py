"""
Garmin MCP Server - Poke Compatible
All 95+ tools from garmin_mcp, served over HTTP.

2026-09-10: an auth failure no longer kills the process.
The server always starts. Authentication runs as a background warmup and is
retried lazily on the first tool call that needs it.
"""
import os
import sys
import threading

# Add src directory to path for module imports
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv

load_dotenv()

from fastmcp import FastMCP
from config import PORT, HOST
from garmin_client import init_garmin_client

# Import all tool modules
from modules import (
    activity_management,
    health_wellness,
    training,
    user_profile,
    devices,
    gear_management,
    weight_management,
    challenges,
    workouts,
    workout_templates,
    data_management,
    womens_health,
)

# Build the proxy. No network call happens here.
garmin_client = init_garmin_client()

MODULES = (
    activity_management,
    health_wellness,
    training,
    user_profile,
    devices,
    gear_management,
    weight_management,
    challenges,
    workouts,
    data_management,
    womens_health,
)

for module in MODULES:
    module.configure(garmin_client)

mcp = FastMCP("Garmin MCP Server")

for module in MODULES:
    mcp = module.register_tools(mcp)

mcp = workout_templates.register_resources(mcp)

# Health check - used for keep-alive pings and for reading auth status.
try:
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request):
        return JSONResponse({"ok": True, **garmin_client.status()})

except Exception as exc:
    print(f"[server] skipping /health route: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    print(f"Starting Garmin MCP Server on {HOST}:{PORT}", file=sys.stderr, flush=True)

    # Warm up auth in parallel with the server bind to cut first-call latency.
    threading.Thread(target=garmin_client.warmup, daemon=True).start()

    mcp.run(
        transport="http",
        host=HOST,
        port=PORT,
        stateless_http=True,
    )
