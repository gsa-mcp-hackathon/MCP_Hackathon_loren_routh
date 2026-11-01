"""Phoenix evaluation harness for the BIM Viewer MCP server.

This package lives at the repo root in `eval/phoenix/`. It is NOT part of the
shipped MCP server — it only exercises and scores it.

Every module loads credentials through `_env.load_eval_env()`, which reads the
GSA_AI_ENV_PATH environment variable and loads the .env it points to. There is
no local .env in this repo.
"""
