"""
Configuration for Garmin MCP Server (Poke-compatible)
"""
import os

# Garmin authentication
GARMINTOKENS_BASE64 = os.getenv("GARMIN_TOKENS_BASE64") or os.getenv("GARMINTOKENS_BASE64")
GARMIN_EMAIL = os.getenv("GARMIN_EMAIL")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD")
GIST_ID = os.getenv("GIST_ID", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "")

# Server settings
PORT = int(os.getenv("PORT", 8000))
HOST = os.getenv("HOST", "0.0.0.0")
