"""Shared environment loading for the eval harness.

Credentials are never stored in this repo. Instead, the user keeps a single
.env elsewhere and points to it with the GSA_AI_ENV_PATH environment variable.
Every eval module calls `load_eval_env()` before reading USAI_* / *_MODEL vars.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

_loaded = False


def load_eval_env() -> None:
    """Load the user's .env via the GSA_AI_ENV_PATH environment variable.

    Idempotent: safe to call from every module. Raises a clear error if the
    variable is unset or points at a missing file, so misconfiguration fails
    loudly instead of silently running with no credentials.
    """
    global _loaded
    if _loaded:
        return

    env_path = os.environ.get("GSA_AI_ENV_PATH")
    if not env_path:
        raise SystemExit(
            "GSA_AI_ENV_PATH is not set. Point it at the .env file that holds "
            "your USAI_API_KEY / USAI_BASE_URL / USAI_MODEL."
        )
    if not os.path.exists(env_path):
        raise SystemExit(
            "GSA_AI_ENV_PATH points to a path that does not exist."
        )

    load_dotenv(env_path)
    _loaded = True


def require(name: str) -> str:
    """Return a required env var's value or exit with a clear message."""
    load_eval_env()
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Required environment variable {name} is not set.")
    return value


def agent_model() -> str:
    """Model slug for the agent-under-test (defaults to USAI_MODEL)."""
    load_eval_env()
    return os.environ.get("AGENT_MODEL") or require("USAI_MODEL")


def judge_model() -> str:
    """Model slug for the LLM-as-judge (distinct default from the agent)."""
    load_eval_env()
    return os.environ.get("JUDGE_MODEL", "claude-sonnet-5.5")


def usai_api_base() -> str:
    """OpenAI-compatible base URL for the USAi gateway (adds /api/v1)."""
    base = require("USAI_BASE_URL").rstrip("/")
    return base + "/api/v1"
