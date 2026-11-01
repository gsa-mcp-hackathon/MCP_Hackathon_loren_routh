# Quickstart — Run the BIM Viewer MCP Server

This gets you from clone to a running server connected to a client, then (optionally)
to the live 3D viewer the server drives.

## 1. Install prerequisites

You need [uv](https://docs.astral.sh/uv/) (a fast Python package manager):

```bash
pip install uv     # or: brew install uv
```

## 2. Set up the project

```bash
cp .env.example .env
uv sync
```

The MCP server itself needs **no credentials** to start — it exposes tools and
waits for a client. (Credentials are only needed to run the evaluation or the live
demo backend; see steps 6–7.)

## 3. Run the server

```bash
uv run python main.py
# or, equivalently, via the installed script:
uv run bim-viewer-mcp
```

The server starts in **stdio** mode and waits for a client to connect over
stdin/stdout. That's correct — it doesn't print a URL, because a local MCP server
is launched *by* the client as a subprocess. Press `Ctrl-C` to stop it.

## 4. Connect a client

### Claude Code

Create or edit `.mcp.json` (or your user MCP settings) with the **absolute path**
to this repo:

```json
{
  "mcpServers": {
    "bim-viewer": {
      "command": "uv",
      "args": ["run", "bim-viewer-mcp"],
      "cwd": "/absolute/path/to/MCP_Hackathon_submission"
    }
  }
}
```

### Claude Desktop

Add the same block to
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows), then restart Claude
Desktop.

Once connected, ask the client to call a tool, e.g.:

> "Use find_by_category to find the air terminals on Level 2, then highlight them."

The resolve tools (`find_by_category`, `find_elements`) return the matching
elements; the act tools (`highlight_elements`, `isolate`, `zoom_to`, `reset`) send
a command to the viewer backend. Without the demo backend running (step 7), the
resolve tools report that the backend could not be reached — tool *selection* still
works, which is what the evaluation scores.

## 5. Verify everything

```bash
uv sync --group dev
uv run pytest tests/ -v      # 45 tests pass
uv run ruff check .          # lint clean
```

## 6. Run the evaluation (optional)

The Phoenix harness measures how well an agent uses the tools. It reads credentials
from the `.env` pointed to by `GSA_AI_ENV_PATH` (see `.env.example` for the
variable names — `USAI_API_KEY`, `USAI_BASE_URL`, `USAI_MODEL`).

```bash
# From eval/phoenix/ — run the agent + judges against the gold dataset.
# Add --repetitions 5 to reproduce the reported 5-rep scores, and
# --no-tools for the baseline that shows the +0.30 tool lift.
uv run python run_experiment.py --dataset-name bim-viewer-eval-0 \
    --system-prompt-version v3 --repetitions 5
```

Full methodology and results are in
[`docs/project/EVALUATION_WRITEUP.md`](docs/project/EVALUATION_WRITEUP.md); a
step-by-step run guide is in
[`docs/project/HOW_TO_RUN_THE_EVAL.md`](docs/project/HOW_TO_RUN_THE_EVAL.md).

## 7. Run the live 3D viewer (optional)

The act tools drive a live APS 3D viewer served by the demo backend in `app/`. The
backend needs a Databricks SQL warehouse in the environment:

```bash
# From app/ — set your Databricks host, warehouse id, and a fresh token first:
#   DATABRICKS_HOST, DATABRICKS_WAREHOUSE_ID, DATABRICKS_TOKEN
uvicorn app:app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000/ and load the model
```

Point the MCP server's tools at this backend by setting `VIEWER_BASE_URL`
(defaults to `http://localhost:8000`). By default the viewer streams the SVF
package from a Databricks UC Volume over `/dbx-models` (`volume_models.py`); a
local copy under `app/ModelDerivatives/` (served at `/models`) is the offline
fallback. Either way the model binaries are large, git-ignored, and required only
for the live demo, not for the server code or the eval.

To sanity-check the full loop (resolve a category, then highlight it in the open
viewer), run one of the probes:

```bash
uv run python scripts/probe_live_viewer.py
```

---

## Next steps

- **Read the tool code** — one file per tool under `src/bim_viewer_mcp/tools/`.
- **See the design rationale** — "Design principles (MCP)" in the
  [README](README.md#design-principles-mcp).
- **Reproduce the eval** —
  [`docs/project/HOW_TO_RUN_THE_EVAL.md`](docs/project/HOW_TO_RUN_THE_EVAL.md).
