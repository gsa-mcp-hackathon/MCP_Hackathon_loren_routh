# BIM Viewer MCP Server

An MCP server that lets an AI agent **find and show mechanical equipment in a live
3D building model**. Ask for a category, a cost ranking, or a level, and the agent
highlights, isolates, or frames exactly those elements in an Autodesk Platform
Services (APS) 3D viewer — and can hand back a procurement-ready list of what was
found.

Built on the [GSA MCP hackathon template](https://github.com/GSA-TTS/mcp-hackathon-template)
with [FastMCP](https://github.com/jlowin/fastmcp), [Pydantic](https://docs.pydantic.dev),
and [uv](https://docs.astral.sh/uv/).

---

## What problem it solves

Facility management (FM) teams — O&M technicians, building managers, procurement
analysts — reconcile asset spreadsheets against drawings by hand. Nothing links a
spreadsheet line to the actual element in the building, so a count or a replacement
list can't be visually verified.

This server closes that gap. An agent resolves a plain-language request ("the air
terminals on Level 2", "equipment over $50k to replace") against the building's
asset data, then **shows the matching elements in the 3D model** so the answer is
verifiable, and can export the set as a procurement list keyed to each element's
replacement cost.

It is **read / view-only by construction**: no tool edits, writes, or deletes the
model.

---

## Repo structure

```
MCP_Hackathon_submission/
├── README.md                  # This file
├── QUICKSTART.md              # Clone → run the server → connect a client
├── SECURITY.md                # Security posture + GSA disclosure policy
├── main.py                    # Local entry point (uv run python main.py)
├── pyproject.toml             # Package + dependencies (uv)
│
├── src/bim_viewer_mcp/        # ← THE MCP SERVER (graded code)
│   ├── app.py                 #   Thin entry: build FastMCP, pick transport
│   ├── config.py              #   Settings from env vars / .env (Pydantic)
│   ├── models.py              #   Pydantic models, enums, allow-lists, id checks
│   ├── utils.py               #   Shared HTTP helper (fixed origin, SSRF guards)
│   ├── routes.py              #   HTTP-only routes (/health)
│   ├── tools/                 #   ONE FILE PER TOOL
│   │   ├── find_elements.py       resolve — by cost (top-N / threshold)
│   │   ├── find_by_category.py    resolve — by named category / level / room
│   │   ├── highlight_elements.py  act — color a set in place
│   │   ├── highlight_rooms.py     act — color rooms
│   │   ├── isolate.py             act — show only a set
│   │   ├── zoom_to.py             act — move the camera
│   │   ├── reset.py               act — clear and show the whole building
│   │   └── color_by.py            (reserved stub)
│   ├── prompts/               #   Reusable conversation starters
│   └── resources/             #   Server metadata the client can read
│
├── eval/
│   ├── phoenix/               # ← THE EVALUATION (Arize Phoenix harness)
│   │   ├── run_experiment.py      run agent + judges against a dataset
│   │   ├── judges/                correctness + scope LLM judges
│   │   ├── prompts/               versioned system prompts (v1–v3)
│   │   └── datasets/              gold questions (evaluation.xml → CSV)
│   └── databricks/            # Databricks eval scaffolding
│
├── tests/                     # 45 import/registration/security smoke tests
│
├── app/                       # The demo web app (viewer backend)
│   ├── app.py                 #   FastAPI backend: asset API + WebSocket hub
│   ├── agent.py               #   Fast tool-using agent (/api/agent)
│   ├── genie.py               #   Databricks Genie path (demo fallback)
│   ├── volume_models.py       #   Streams the SVF package from a Databricks
│   │                          #   UC Volume over /dbx-models (Range-aware proxy)
│   ├── db.py, chat.py, fields.py
│   ├── static/                #   The 3D viewer (viewer.html) + assets
│   └── ModelDerivatives/      #   3D model derivatives (local fallback at /models;
│                              #   the viewer streams from a Databricks UC Volume
│                              #   via /dbx-models by default — large binaries,
│                              #   git-ignored; local demo only)
│
├── data_prep/                 # Scripts that build the synthetic asset data
│   ├── generate_ca8888_synthetic_maximo.py
│   ├── pare_revit_json_for_upload.py
│   └── setup.sql
│
├── scripts/                   # Manual end-to-end probes against a live viewer
│   ├── probe_live_viewer.py
│   └── probe_transformers.py
│
└── docs/project/              # Evaluation write-up, run guide, journey, security notes
```

The **MCP server** (`src/bim_viewer_mcp/`) and the **evaluation**
(`eval/phoenix/`) are the deliverable code. The `app/` directory is the live
demo the server drives.

---

## How the pieces fit together

```
Revit / BIM model (JSON)  ─┐
                           ├─►  Databricks warehouse  ─►  app/ backend  ─►  MCP server  ─►  3D viewer
Asset & cost data (synth) ─┘    (joined on asset id)      (asset API +      (find → act    (highlights the
                                                           WebSocket hub)     tools)         matching elements)
```

- The model and the synthetic asset/cost data are joined in Databricks on a shared
  asset id; each asset row carries the element's durable Revit UniqueId
  (`source_unique_id`), which is the viewer's `externalId`.
- The **MCP server's resolve tools** (`find_elements`, `find_by_category`) ask the
  `app/` backend for the elements that match a request and get back their
  `externalId`s.
- The **act tools** (`highlight_elements`, `isolate`, `zoom_to`, `reset`) POST a
  validated command to the backend, which broadcasts it to every connected viewer
  over a WebSocket — so the open 3D model lights up.

The asset/cost data is **synthetic, Maximo-style data**, not a connection to a real
Maximo system.

---

## Getting started

### Prerequisites
- [uv](https://docs.astral.sh/uv/) — `pip install uv` or `brew install uv`

### Run the MCP server

```bash
cp .env.example .env
uv sync
uv run python main.py
```

The server starts in **stdio** mode — it talks JSON-RPC over stdin/stdout, which
is how local clients (Claude Desktop, Claude Code) launch it. See
[QUICKSTART.md](QUICKSTART.md) to connect a client.

### Verify

```bash
uv sync --group dev
uv run pytest tests/ -v      # 45 tests
uv run ruff check .          # lint
```

### Run the demo app (optional, for the live 3D viewer)

The `app/` backend serves the viewer and the asset API the resolve tools call.
It needs a Databricks SQL warehouse (host, warehouse id, token) in the
environment. From `app/`:

```bash
uvicorn app:app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000/
```

By default the viewer streams the SVF package from a Databricks UC Volume over
`/dbx-models` (a Range-aware proxy in `volume_models.py`), so the large model
binaries do not need to live on the local disk. A local copy under
`app/ModelDerivatives/` (served at `/models`) is the offline fallback. Either way
the derivatives are required only for the live demo, not for running or grading
the server code.

---

## Design principles (MCP)

- **Validated inputs.** Tool parameters are Pydantic-typed with `Literal`
  allow-lists for categories and levels — the model cannot pass a raw `asset_type`,
  free-form field, or SQL.
- **Tools map to goals, not endpoints.** The surface splits into **resolve** (find
  which elements a request is about) and **act** (drive the viewer). This
  "find, then act" shape is what an FM user actually does, not a thin wrapper
  around one REST call.
- **Durable ids only.** Elements are addressed by their Revit UniqueId (the
  viewer's `externalId`), never a transient numeric id.
- **Read / view-only.** No tool edits, writes, or deletes. The viewer-command
  surface is an explicit allow-list; the backend origin is fixed by server config,
  never taken from a tool argument.
- **Sanitized failures.** Tools return an error dict with a `hint`, never a raw
  stack trace or upstream body.

---

## Evaluation

The server is evaluated with an [Arize Phoenix](https://docs.arize.com/phoenix)
harness (see `eval/phoenix/`). Ten gold questions span category resolves, cost
rankings, camera moves, resets, and out-of-scope refusals; two LLM judges score
**correctness** (right tool, right targets) and **scope** (refuse out-of-authority
requests). Re-running with the tools hidden isolates the server's contribution:
**+0.30 correctness** (0.70 → 1.00), scope 1.0 in both configurations.

Full methodology, results, and security analysis:
[`docs/project/EVALUATION_WRITEUP.md`](docs/project/EVALUATION_WRITEUP.md).
To reproduce a run: [`docs/project/HOW_TO_RUN_THE_EVAL.md`](docs/project/HOW_TO_RUN_THE_EVAL.md).

---

## Security

Read/view-only by construction, synthetic data only (no PII/CUI), credentials never
committed. See [SECURITY.md](SECURITY.md) and
[`docs/project/SECURITY-NOTES.md`](docs/project/SECURITY-NOTES.md).

---

## License

[MIT](LICENSE).
