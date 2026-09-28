"""HTTP server configuration. Authentication is token-only; see .env.example."""
import os

PORT = int(os.getenv("PORT", 8000))
HOST = os.getenv("HOST", "0.0.0.0")
