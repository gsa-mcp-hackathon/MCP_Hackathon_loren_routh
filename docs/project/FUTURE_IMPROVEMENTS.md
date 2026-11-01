# Future Improvements — BIM Viewer MCP (post-hackathon backlog)

Running list of ideas to revisit **after** the 11/20 hackathon submission. Nothing
here is in scope for the hackathon deliverables unless explicitly promoted.

> How to use this: each item has a short **what / why / open questions** so we can
> pick it up cold later. Add freely; we triage when the hackathon is in.

---

## 1. Live Autodesk model ingestion (AFG — Autodesk for Government / FedRAMP BIM 360)

**What:** Instead of serving pre-exported SVF files from disk, connect to the
FedRAMP'd "Autodesk for Government" (AFG) tenant, pull the Revit models and their
**Model Derivative** translations through the API, and load them in our MCP viewer
dynamically.

**Why:** Removes the manual RevitExtractor export step; lets the viewer show the
*current* model version straight from BIM 360 / ACC; sets up for always-fresh data
and version history.

**What we have:** Credentials + admin rights on the AFG tenant, AND a working .NET
proof-of-concept that already does the upstream half end-to-end. See
`C:\Users\lorenerouth\source\repos\AFGAutomation` (project `Afg.Aps.Client` +
`Afg.Aps.TestApi`) and its write-up
`Afg.Aps.Client\AFG_Documentation.md`.

### What the PoC already PROVED on AFG (confirmed facts, not assumptions)

- **Base URL:** `https://api.afg.us.autodesk.com` (this is the AFG/FedRAMP host —
  NOT the commercial `developer.api.autodesk.com`).
- **Auth:** 2-legged OAuth (`client_credentials`) works. Token endpoint
  `POST /authentication/v2/token`, HTTP Basic (clientId:secret), scopes granted:
  `bucket:create bucket:read data:read data:write data:create viewables:read`.
  Token ~30 min (`expiresIn` ~1799s).
- **OSS (object storage) works:** create/list buckets (`/oss/v2/buckets`),
  upload objects. A 92 MB RVT **failed single-shot upload** with nginx
  `413 Request Entity Too Large`, so the PoC uses **resumable/chunked upload**
  (`/oss/v2/buckets/{bucket}/objects/{name}/resumable`, 5 MB chunks, `Session-Id`
  header) — that succeeded.
- **Model Derivative works:** submit job `POST /modelderivative/v2/designdata/job`
  with output `type = "svf2"`, views `["2d","3d"]`; poll manifest
  `GET /modelderivative/v2/designdata/{urn}/manifest` until
  `status = success / progress = complete`. The `{urn}` is the base64url of the
  OSS `objectId`. Confirmed success on `Snowdon Towers Sample Architectural.rvt`.
- **Manifest gives us an inventory:** 2D sheets + 3D views, each with a `guid`,
  `viewableID`, `name`, `role`, `phaseNames`, `ViewSets`, `status`. This is a
  rich source of view/sheet structure (and possibly the externalId/property
  mapping we care about — see "metadata" unknown below).
- **A derivative-download call exists** in code
  (`GET /modelderivative/v2/designdata/{sourceUrn}/manifest/{derivativeUrn}`) but
  was **never exercised end-to-end** — we proved upload + translate, not download
  + render.

### CRITICAL known limitation (the blocker for our viewer)

AFG translates to **SVF2**, but our current viewer loads **classic SVF**:

- Our viewer (`static/viewer.txt` → served as the live viewer) uses
  `Autodesk.Viewing` with `env: "Local"` and `viewer.loadModel(<path>.svf)`,
  reading classic **SVF** files (produced locally by **RevitExtractor**). The
  package now lives in a Databricks UC Volume and is streamed to the viewer over
  the `/dbx-models` Range-aware proxy (`volume_models.py`), with a local
  `ModelDerivatives/` copy served at `/models` as the offline fallback. SVF =
  older, self-contained, static-file-friendly format — so it serves cleanly over
  either path.
- AFG's Model Derivative produces **SVF2** = newer streaming format, normally fed
  to the viewer by Autodesk's online **modeldata/OTG** service rather than served
  as plain static files.
- The PoC **confirmed the interactive Viewer does NOT work against AFG**:
  - commercial `https://cdn.derivative.autodesk.com/modeldata/manifest/{urn}`
    → **401** (AFG token rejected by the commercial CDN).
  - AFG `https://api.afg.us.autodesk.com/modeldata/manifest/{urn}` → **404**
    ("resource does not exist"). The streaming `modeldata` endpoint SVF2 needs is
    simply not exposed (or not documented) on AFG today.
- **Implication:** we cannot assume the commercial "point the viewer at a URN +
  token" path. The realistic path is **download the derivative bundle and
  self-serve it**, exactly like our current SVF package (streamed from the UC
  Volume over `/dbx-models`, with the local `/models` static mount as fallback) —
  BUT it is
  an open question whether a downloaded **SVF2** bundle renders from local static
  files at all, since SVF2 is built to stream from the modeldata service that AFG
  doesn't expose.

### Note: Revit version does NOT make SVF and SVF2 the same

The user asked whether running RevitExtractor in Revit 2027 should "match" the
AFG output. No — the Revit version doesn't determine the derivative format. The
**tool** chooses it: RevitExtractor emitted classic **SVF** locally; the AFG job
requested **SVF2**. They are different formats regardless of Revit version.

### Remaining open questions (ask AFG support / test on tenant)

Already drafted in `AFGAutomation\Afg.Aps.Client\Questions for AFG Support.txt`:
is the browser Viewer supported on AFG at all; correct `viewer3D.min.js` /
`style.min.css` URLs; correct `env`/`api` Initializer values; the AFG `modeldata`
endpoint; CORS from localhost/agency domains; and whether AFG supports SVF2
*viewing in browser* or only Model Derivative *processing*. Plus:
- Does AFG expose Model Derivative **metadata/properties** endpoints
  (`/modelderivative/v2/designdata/{urn}/metadata` and `.../metadata/{guid}/properties`)?
  If so, that could supply the externalId↔property mapping directly. **Untested.**
- Can a **classic `svf`** output type be requested from AFG Model Derivative
  (instead of `svf2`)? Classic SVF self-serves cleanly today, so this would be the
  lower-risk path if AFG allows it.
- Data-classification / egress rules for pulling model bytes out of the FedRAMP
  boundary onto a local/dev machine (coordinate with security before downloading).

### Recommended first post-hackathon spike (concrete, ordered)

1. **Exercise the download** the PoC already has code for: fetch the derivative
   bundle for the Snowdon model and inspect what actually comes down (manifest
   `derivatives[].children[]` → the SVF2 resource URNs).
2. **Attempt local render:** drop the downloaded bundle into a `ModelDerivatives`
   -style folder and try to load it with our existing `env: "Local"` viewer. This
   directly answers "does downloaded AFG SVF2 render from static files?"
3. **If SVF2 won't self-serve:** ask AFG support (a) whether browser SVF2 viewing
   is supported + correct Initializer config, and (b) whether classic `svf` output
   can be requested. Fall back to classic SVF if available.
4. **Only then** design the live-ingestion service (auth → resolve version →
   translate if needed → poll → download → serve), porting the proven .NET flow
   into our Python backend (or calling the existing .NET service).

**Status:** Upstream (auth/upload/translate/manifest) PROVEN on AFG via the .NET
PoC. Blocked downstream on the SVF2-vs-local-SVF viewer question above; that's the
spike to run first.

---

## 2. Maximo data ingestion (batch / file dump — no streaming)

**What:** Bring Maximo asset + work-order data into the system. Per the user, the
Maximo feed is **a batch dump or spreadsheet** — there is **no streaming or
on-demand** path to Maximo. So this is a one-way, periodic import, not a live
connection.

**Why:** Maximo is the authoritative source for the asset/maintenance facts the
viewer reasons over (replacement cost, maintenance cost, asset type, etc.). Today
those facts live in Databricks tables (`users.loren_routh.maximo_assets`,
`users.loren_routh.maximo_workorders`) — the naming already signals a
Maximo-origin. This item makes the refresh path explicit and repeatable.

**What we know today:**
- The backend already reads Maximo-named tables from Databricks
  (`app.py`: `ASSETS_TABLE`, `WORKORDERS_TABLE`). So the *consumption* side exists;
  what's undefined is the *load* side — how a new Maximo dump becomes those tables.
- The durable join key is `source_unique_id` (= Revit UniqueId = APS externalId).
  Any Maximo import must preserve/carry that key so assets map back to model
  elements. (If a raw Maximo dump doesn't contain it, we need a mapping step.)

**Open questions / decisions deferred:**
- **Load mechanism is intentionally left open for now** (user's call). Candidate
  shapes to decide later:
  - manual file → script → Databricks table (keeps Databricks the single source
    the viewer queries); or
  - viewer backend reads the spreadsheet directly off disk (simpler, but splits
    the source of truth away from Databricks).
  Leaning toward Databricks-as-source for consistency with item #4, but not
  decided.
- Cadence (how often a new dump arrives), and whether old versions are retained.
- File format(s) Maximo will actually produce (CSV vs. XLSX), column names, and
  whether `source_unique_id` is present or must be derived.
- Validation on import (row counts, required columns, key coverage against the
  model) so a bad dump can't silently corrupt the asset view.
- Data classification of the Maximo dump and where the file may live.

**Status:** Idea only. Mechanism deliberately undecided; capture the
batch/no-streaming constraint and revisit after the hackathon.

---

## 3. Consume PowerBI data

**What:** Pull data **out of** PowerBI (datasets / reports / curated measures) to
enrich what the viewer and agent can reason over. Direction is **inbound** — we
are a *consumer* of PowerBI-curated data, not primarily a data source feeding
PowerBI dashboards.

**Why:** PowerBI may already hold cleaned, modeled, or aggregated agency data
(KPIs, roll-ups, curated joins) that would be expensive to re-derive. Consuming it
lets the viewer surface those numbers against the 3D model without rebuilding the
logic.

**Open questions / unknowns (verify, do NOT assume — esp. in a gov/FedRAMP context):**
- **Which PowerBI?** Commercial Power BI Service vs. a government cloud
  (Power BI for US Government / GCC). Endpoints, auth, and available APIs differ —
  must confirm the tenant before building.
- **Access path:** Power BI REST API (`datasets`, `Execute Queries` via DAX),
  an exported dataset/dataflow, XMLA endpoint, or just a scheduled export file.
  No path chosen yet.
- **Auth:** service principal vs. delegated user; which scopes; whether the tenant
  permits API access at all.
- **Shape of the data we actually want** — is it a measure/table pulled on demand,
  or a periodic export landing alongside the Maximo batch (item #2)? Given the
  "batch, no streaming" theme for source data, a periodic export may be the
  natural fit.
- How consumed PowerBI data joins to assets — does it carry `source_unique_id` or
  another key we can map?

**Status:** Idea only. Confirm tenant type and access path first.

---

## 4. Databricks as the central data hub

**What:** Make Databricks the **hub** that the other sources flow into and the
viewer/agent query from — a bigger role than it plays today. Maximo batch dumps
(#2) and consumed PowerBI data (#3), plus AFG model-derived data (#1), would land
in Databricks tables; the backend reads only from Databricks.

**Why:** One consistent, governed, queryable source of truth. The backend already
queries Databricks for the cost/category tools, so this extends a pattern that
works rather than inventing a new one. It also keeps joins (asset ↔ model element
↔ work order ↔ external KPIs) in one place keyed on `source_unique_id`.

**What we know today:**
- Databricks already works and backs the live tools
  (`/api/assets/query`, `/api/assets/by-category`) via SQL warehouse
  `50d2bd4ddf65e0f5` on host `dbc-aae0ba16-8727`.
- Tables are under `users.loren_routh.*` today — a personal schema, fine for the
  pilot but something to formalize into a proper governed schema/catalog as this
  grows.

**Open questions / decisions deferred:**
- Target schema/catalog design (move off a personal `users.loren_routh` schema to
  a shared, access-controlled one).
- How each source lands (batch load jobs for Maximo/PowerBI; model data from AFG)
  and the refresh cadence/orchestration.
- Governance: who can read/write, data classification per table, retention.
- Keep `source_unique_id` as the canonical join key across every table.
- Interaction with item #2's "load mechanism" decision — if we commit to
  Databricks-as-hub, that likely settles #2 toward "file → Databricks table."

**Status:** Direction, not a task yet. Depends on decisions in #2 and #3;
revisit together after the hackathon.

---

## 5. Broader interoperability vision (other sources & systems)

**What:** A catch-all for the larger goal: the viewer/agent interoperating with
**other** agency sources and systems beyond Maximo, PowerBI, and AFG — e.g.
additional asset/work-management systems, GIS, document/records systems, or other
data warehouses.

**Why:** The hackathon deliverable is one slice (BIM viewer + MCP tools over a
pilot dataset). The real vision is a hub (item #4) that many systems feed and that
an AI agent can reason across. Capturing this keeps the big picture visible so
individual items (#1–#4) are understood as pieces of it.

**Guiding principles to carry forward (from how we've worked so far):**
- **Batch-first for source data** — the known sources (Maximo, and likely PowerBI)
  are periodic dumps, not streams. Design for refreshes, not live feeds, unless a
  specific source proves otherwise.
- **One canonical key** — `source_unique_id` (Revit UniqueId = APS externalId)
  ties model elements to every external record. Any new source must map to it.
- **Databricks as the convergence point** (item #4) rather than many point-to-point
  integrations into the viewer backend.
- **Verify gov/FedRAMP specifics, never assume commercial behavior** — the AFG
  lesson (commercial endpoints ≠ gov endpoints) applies to every government
  system we integrate.
- **Server-side allowlists for anything the agent can trigger** — keep the posture
  used by the cost/category tools (no caller-supplied SQL/paths; fixed allowlists).

**Status:** Vision / umbrella entry. No task; a home for future sources as they
come up.

---

## 6. Agent-driven Revit model generation & programmatic updating

**What:** A separate project the user is already building: **generate and
programmatically update Revit models from table data.** The envisioned tie-in is
that this MCP/Databricks project's **Agent** could drive that generation — i.e. a
user instruction ("create/update these elements") becomes table data that the
Revit generator consumes, producing or updating a real model.

**Why:** Closes the loop from *reading/understanding* a model (what the hackathon
viewer does) to *authoring/maintaining* one. Combined with items #1–#4, the vision
is: external data (Maximo/PowerBI/CAD) → a governed store → an agent that can both
**answer about** and **author** BIM, with the generated model flowing back through
AFG Model Derivative (#1) into the viewer.

**What the user has already PROVEN (separate project — facts, not assumptions):**
- Can generate a **LOD300** Revit model from **table data** derived from
  **processed CAD files**.
- Uses a **PostGIS** database + **Python scripts** as the **system of record and
  change-tracking** layer — not just staging. PostGIS's spatial functions
  (the `ST_*` library) are used to **query and manipulate** the data (geometry
  footprints, spatial relationships), and the scripts emit **table data** that the
  **RevitModelGenerator** consumes to build/update the model. Had real success
  with this round-trip.
- **RevitModelGenerator runs INSIDE Revit** via the **Revit API** (it is an
  add-in/script that needs a Revit host, not a headless/file-only generator).

**Architecture sketch (as understood):**

```
CAD files → (processing) → table data
         → PostGIS (system of record + ST_* spatial query/manipulate + change tracking)
         → table data out
         → RevitModelGenerator (Revit API, runs inside Revit) → LOD300 RVT (create/update)
```

Envisioned addition: an **Agent** (this project) sits in front, turning a
plain-language instruction into the table data / PostGIS operations that feed the
generator — and the resulting RVT can then go through AFG Model Derivative (#1)
back into the viewer.

**The key architectural tension to design around:**
- The generator **must run inside Revit (Revit API)**. An MCP/agent service is a
  web/Python backend. So the agent cannot "just call" the generator in-process —
  there has to be a **bridge** to a Revit host. Options to evaluate later:
  - a queue/table the agent writes to and a Revit-side listener (pyRevit / an
    add-in polling PostGIS or a job table) picks up — fits the "table data in/out"
    pattern already proven;
  - Autodesk **Design Automation for Revit** (cloud, headless) IF available on
    **AFG** (unknown — commercial DA ≠ gov; verify, do not assume);
  - a human runs the Revit step (propose-only agent) — safest first cut.
- **Agent authority is intentionally LEFT OPEN** (user's call). Capture both
  postures without choosing:
  - **Propose-only / human-in-the-loop:** agent emits/edits table data or PostGIS
    rows; a person reviews and runs the actual Revit generation. Lower risk; good
    first posture given models are authoritative artifacts.
  - **Agent-triggered:** agent can kick off generation/update through
    **allowlisted, server-side** operations (same posture as the cost/category
    tools — no free-form SQL/paths, fixed operations, bounded inputs).
  Whichever is chosen, authoring operations are far higher-stakes than the current
  read/highlight tools, so safety/review/audit need explicit design.

**Open questions / unknowns:**
- Where PostGIS fits relative to the Databricks hub (#4): does PostGIS stay the
  model-authoring system of record (its `ST_*` spatial ops have no direct
  Databricks equivalent), with Databricks as the analytics/interop hub? Likely two
  stores with a defined sync, not one — needs a deliberate boundary.
- The Revit-host bridge mechanism (see tension above) and whether Design
  Automation for Revit exists on AFG.
- What "LOD300 from table data" requires as input schema — define the table
  contract the agent would have to produce.
- Validation/review/audit for agent-authored model changes (far stricter than
  read-only tooling).
- Round-trip identity: generated elements must carry a durable `source_unique_id`
  so they map through AFG derivatives (#1) back into the viewer and to Maximo (#2).

**Status:** Separate proven project (CAD→PostGIS→Revit round-trip works). The
*agent-driven* layer is the new idea here — gated on the Revit-host bridge and the
authority decision, both deferred. Revisit after the hackathon.

---
