"""Server configuration loaded from environment variables or a .env file.

Every field can be overridden by an environment variable of the same name
(upper- or lower-case). Copy `.env.example` to `.env` for local development.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """MCP server settings. Override any field via environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Transport ─────────────────────────────────────────────────────────────────────
    # "stdio"           — local subprocess (Claude Desktop / Claude Code / Zed)
    # "streamable-http" — remote HTTP server (containers, cloud deploys)
    #
    # NOTE: app.py also auto-selects HTTP when a platform injects DATABRICKS_APP_PORT
    # or PORT, so you usually do NOT need to set this on a cloud host.
    mcp_transport: str = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000

    # ── Viewer backend ────────────────────────────────────────────────────────────────
    # Fixed, operator-controlled origin of the BIM viewer's web backend. The
    # tools POST validated viewer commands here; the backend broadcasts them to
    # every connected 3D viewer over its WebSocket. This is NEVER taken from a
    # tool argument — keeping it operator-controlled is the core SSRF guard.
    # Override with VIEWER_BASE_URL in the environment.
    viewer_base_url: str = "http://localhost:8000"


settings = Settings()
