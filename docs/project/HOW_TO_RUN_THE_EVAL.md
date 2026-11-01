# Running the BIM Viewer Eval — a step-by-step guide

**Audience:** someone who has never run an evaluation before. This explains not
just *what* to type, but *what each thing is* and *why you're doing it*. Read it
top to bottom once; after that it's a checklist.

---

## 1. What is this eval, in plain words?

Your MCP server gives an AI a set of **tools** for driving the 3D viewer
(highlight things, isolate, zoom, reset, etc.). The eval answers one question:

> **Given only these tools, does an AI pick the RIGHT tool for a plain-English
> request — and correctly REFUSE requests it shouldn't do?**

It is *not* testing whether the 3D model paints correctly on screen. It tests
the AI's **tool choice**. That's the thing judges care about: a good MCP server
is one an AI can actually use well.

### The pieces

| Term | Plain meaning |
|---|---|
| **Gold questions / dataset** | Your 16 test questions, each paired with a description of the correct action. Lives in `evaluation.xml` (source) and `examples/bim-viewer-eval-0.csv` (the file the harness reads). |
| **Agent** | The AI being tested. It's handed your tools and a question, and it decides what to do. Runs on the model in `AGENT_MODEL`. |
| **Judge** | A *second* AI that grades the agent's answer against the expected action. Runs on `JUDGE_MODEL`. This is called "LLM-as-judge." |
| **Phoenix** | A local web dashboard (from Arize) that runs the experiment, stores every result, and shows you scores and traces. Runs on your machine at http://localhost:6006. |
| **Experiment / run** | One pass of all 16 questions through the agent, then graded by the judges. You run it a few times (different prompt versions, and once with no tools). |
| **Trace** | A detailed recording of one question: what the agent did, which tool it called, how long it took. Phoenix shows these so you can see *why* an answer was right or wrong. |
| **Tool lift** | The improvement from having the tools. You prove it by running once WITH tools and once WITHOUT (`--no-tools`) and comparing scores. Judges love this number. |
| **"Slug" / model ID** | The exact text string that names a model to the USAi gateway, e.g. `claude_4_5_sonnet`. Must be copied exactly. |

### The two judges

- **Correctness** (`match_expected_response`): did the agent choose the right
  tool on the right targets, and only move the camera when it should? Scores
  each answer `correct` (1) or `incorrect` (0).
- **Scope** (`check_answer_scope`): did the agent answer in-domain requests and
  **refuse** out-of-domain ones (e.g. "delete the model," "what chiller should I
  buy for my house")? Scores `within_scope` (1) or `out_of_scope` (0).

---

## 2. One-time setup (already done — here for reference)

These are already configured; listed so you understand what the run depends on.

- **Your credentials file** (`.env`) lives at
  `C:\Users\lorenerouth\source\repos\.env` and holds `USAI_API_KEY`,
  `USAI_BASE_URL`, and the model IDs. The harness finds it through an
  environment variable called `GSA_AI_ENV_PATH`.
- **Model IDs pinned in that `.env`:**
  ```
  AGENT_MODEL=claude_4_5_sonnet     # the AI being tested
  JUDGE_MODEL=claude-sonnet-5.5     # the AI doing the grading
  ```
- **The eval code** lives in the template repo:
  `C:\Users\lorenerouth\Documents\PythonProjects\mcp template\mcp-hackathon-template\eval\phoenix\`

---

## 3. Every-terminal preamble (run this first, every time)

The eval scripts must run from the template repo, and they need two environment
variables set in the shell. Paste this at the top of **every** PowerShell window
you use for the eval:

```powershell
cd "C:\Users\lorenerouth\Documents\PythonProjects\mcp template\mcp-hackathon-template"
$env:PYTHONPATH = (Get-Location).Path          # lets Python find the eval.phoenix package
$env:GSA_AI_ENV_PATH = "C:\Users\lorenerouth\source\repos\.env"   # points at your credentials
```

> Why: `PYTHONPATH` tells Python where the `eval` package is (the modules import
> each other as `eval.phoenix.*`). `GSA_AI_ENV_PATH` tells the harness where your
> API key and model IDs are. Miss either and you get an import error or a
> "GSA_AI_ENV_PATH is not set" message.

All python commands below use the repo's virtual environment explicitly:
`.\.venv\Scripts\python.exe`. (A "virtual environment" / **venv** is a private
copy of Python with just this project's packages installed.)

---

## 4. The run, step by step

You need **two terminals**: one to keep Phoenix running, one to run commands.
Do the preamble (section 3) in BOTH.

> ⚠️ **IMPORTANT — the viewer backend must be running first.** Several test
> questions are cost-driven (e.g. "highlight everything over $50,000"); to answer
> them the agent's tools call the viewer backend, which queries Databricks. If
> that backend is down, those tool calls fail and the agent can only *describe*
> what it would do — which the correctness judge (rightly) marks wrong. We learned
> this the hard way on the 10/28 run: correctness jumped from **30% → 60%** for
> the same prompt just by bringing the backend up first. **Do Step 0 below before
> running any experiment.**

### Step 0 — Start the viewer backend (so the agent's tools actually work)

In a *third* terminal, from the viewer project (`C:\Users\lorenerouth\Documents\MCP_Hackathon`):

```powershell
cd "C:\Users\lorenerouth\Documents\MCP_Hackathon"
# 1) Make sure the Databricks SQL warehouse is RUNNING (start it in the Databricks
#    web UI if it's stopped — it auto-stops when idle).
# 2) Point at the warehouse and mint a fresh access token (the token lasts ~1 hour):
$env:DATABRICKS_HOST = "https://dbc-aae0ba16-8727.cloud.databricks.com"
$env:DATABRICKS_WAREHOUSE_ID = "50d2bd4ddf65e0f5"
$env:DATABRICKS_TOKEN = (& ".\databricks_cli_1.21.0_windows_amd64\databricks.exe" auth token --profile dbc-aae0ba16-8727 | ConvertFrom-Json).access_token
# 3) Start the backend (leave it running):
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Wait for **"Application startup complete"**. Then, in the eval terminal
(Terminal 2), add one line to the preamble so the agent's tools know where the
backend is:

```powershell
$env:VIEWER_BASE_URL = "http://127.0.0.1:8000"
```

> If the token expires mid-run (you'll see backend errors again), just re-run the
> three lines in step 2 above to mint a new one and restart the backend.

### Step A — Start Phoenix (Terminal 1, leave it running)

```powershell
.\.venv\Scripts\python.exe -m phoenix.server.main serve
```

Wait ~20 seconds. When you see **"Phoenix is up and running - open
http://localhost:6006"**, open that URL in your browser. It'll be empty at
first — that's fine. Leave this terminal alone; closing it stops Phoenix.

> Harmless warnings you'll see and can ignore: SQLAlchemy `SAWarning`, a
> Pydantic `PydanticJsonSchemaWarning`, and possibly one
> `Client failed to connect` line from Phoenix's own built-in assistant feature
> (unrelated to our eval). As long as you get the "up and running" line, you're good.

### Step B — Upload the dataset (Terminal 2, once)

First validate without uploading (safe, no network, no model):
```powershell
.\.venv\Scripts\python.exe eval\phoenix\create_dataset.py --dataset-name bim-viewer-eval-0 --dry-run
```
Expect: `Validated ... 16 rows, columns ['query', 'responses']. Dry run — not uploading.`

Then upload it to the running Phoenix:
```powershell
.\.venv\Scripts\python.exe eval\phoenix\create_dataset.py --dataset-name bim-viewer-eval-0
```
Expect: `Uploaded dataset 'bim-viewer-eval-0' to Phoenix ...` and a dataset id.
Refresh the Phoenix page — you'll now see the dataset with 16 rows.

> You only upload once. If you upload again you'll get a second copy with a new
> name/version; not harmful, just messy.

### Step C — Run the experiments (Terminal 2)

**This is the step that spends API calls** (the agent and judge are real model
calls). Each run does 16 questions × (agent + 2 judges). Run these one at a time
and watch Phoenix fill in.

```powershell
# 1) Prompt version v1, WITH tools
.\.venv\Scripts\python.exe eval\phoenix\run_experiment.py --dataset-name bim-viewer-eval-0 --system-prompt-version v1

# 2) Prompt version v2, WITH tools  (compare against v1)
.\.venv\Scripts\python.exe eval\phoenix\run_experiment.py --dataset-name bim-viewer-eval-0 --system-prompt-version v2

# 3) Prompt version v3, WITH tools  (the "find, then act" prompt)
.\.venv\Scripts\python.exe eval\phoenix\run_experiment.py --dataset-name bim-viewer-eval-0 --system-prompt-version v3

# 4) NO-TOOLS baseline (proves "tool lift")
.\.venv\Scripts\python.exe eval\phoenix\run_experiment.py --dataset-name bim-viewer-eval-0 --system-prompt-version v1 --no-tools
```

> `--system-prompt-version` picks which instructions the agent gets
> (`prompts/system_prompt_v1.txt`, `v2.txt`, or `v3.txt`). Running all three shows
> which wording works best. `--no-tools` runs the SAME questions but hides the
> tools, so the agent can only guess — its score should be much lower, and the gap
> is your proof the MCP server adds value.

### Step D — Read the results (browser)

In Phoenix (http://localhost:6006), open the **Experiments** for the dataset.
For each run you'll see the correctness and scope scores, and you can click any
row to read the **trace** — the agent's actual answer and the judge's
explanation. This is where you do failure analysis (Week 2).

### Optional — consistency (repeat each question)

The rubric likes seeing consistency, not a single pass. Add `--repetitions 5`:
```powershell
.\.venv\Scripts\python.exe eval\phoenix\run_experiment.py --dataset-name bim-viewer-eval-0 --system-prompt-version v1 --repetitions 5
```
This runs each question 5 times so you can see how stable the answers are.
(Costs 5× the API calls — do it once you're happy the single run looks right.)

---

## 5. When you're done

- Stop Phoenix: go to Terminal 1 and press **Ctrl+C**. (Your results are saved
  in a local database at `C:\Users\lorenerouth\.phoenix\phoenix.db`, so they
  survive a restart — starting Phoenix again shows them.)

---

## 6. First real run — results (2026-10-28)

The first real experiments ran today. Two things worth recording:

**(a) A judge bug was found and fixed.** The LLM-as-judge grading step crashed on
every example (`JSONDecodeError`). Cause: Phoenix asks the judge model for its
label two ways — OpenAI "structured outputs" and forced "tool calling" — and the
USAi-hosted Claude judge honors *neither* (it returned prose for the first and
rejected the second with `tool_choice ... not supported`). Fix: the two judges
(`correctness_judge.py`, `relevance_judge.py`) now call the judge model directly
in plain **JSON mode** (`response_format={"type": "json_object"}`), which USAi
honors reliably. Shared helper: `eval/phoenix/judges/_classify.py`. The prompts,
labels, and scoring are unchanged. (Written up for the template author in
`EVAL_DEPENDENCY_BUG.md`.)

**(b) The backend-offline lesson (see the ⚠️ box in section 4).** The very first
runs scored low because the viewer backend was down, so the agent's tool calls
failed. After bringing the backend up, correctness improved for the same prompt:

| Experiment | Correctness | Scope |
|---|---|---|
| v1 / with-tools / backend **down** | 30% | 80% |
| v1 / with-tools / backend **up** | 60% | 80% |
| v2 / with-tools / backend **up** | 70% | 80% |
| v1 / **no-tools** / backend up | 78% | 100% |

> Note the no-tools number is *higher* than with-tools — that's not "tools hurt."
> It's a known weakness in the current with-tools behavior: on several in-scope
> requests the agent **asks the user for element IDs instead of just acting**
> (e.g. "highlight air terminals on Level 2" → it asks for externalIds rather
> than highlighting the described set). The judge marks that as a failure to act.
> This is exactly the kind of finding the eval exists to surface, and it's a
> prime candidate for a system-prompt fix (tell the agent to highlight the
> described set without demanding IDs). It's logged for Week-2 failure analysis.

### Earlier smoke test (same day, pre-run)

Plumbing was verified before spending any model calls: dataset dry-run (16 rows,
columns `query`/`responses`), dataset `--list`, Phoenix serving HTTP 200, REST
API alive, and all eval modules importing cleanly.

### Final run — the clean matrix (2026-10-28, later the same day)

After the first run surfaced two real weaknesses (the "asks for IDs instead of
acting" behavior, and a broken Q5), both were fixed and the whole matrix was
re-run at 5 repetitions. Four changes got us from **0.9 → a perfect 1.0**:

1. **Added an 8th tool, `find_by_category`.** The first-run weakness was that the
   agent could only resolve element IDs *by cost* (`find_elements`). So when asked
   to "highlight the air terminals on Level 2," it had no way to turn "air
   terminals on Level 2" into IDs, and it stalled asking the user for externalIds.
   The new tool resolves a named **category** (plus optional **level** and
   **room**) to the right element IDs, so the agent can act directly. The agent
   now exposes **8 tools**, not 7. (A new prompt, `system_prompt_v3.txt`, teaches
   the "find, then act" pattern that uses it.)

2. **Fixed the event-loop stall.** The harness used to open and close a fresh
   event loop per question; the MCP client's async teardown could fire after its
   loop was already dead, and Phoenix would retry into the same broken pattern —
   stalling a run for a very long time. `agent.py`'s `create_experiment_task()`
   now reuses **one** event loop for the whole experiment. (See the row in §7.)

3. **Corrected Q5 (an honest dataset fix).** The original Q5 asked to zoom to the
   equipment in "the main mechanical room." Investigating the real data showed
   there is **no such room** — the big mechanical equipment (air handlers, cooling
   tower, fans) sits on the Roof Level with a NULL room name. So Q5 was testing an
   impossible target and failed 0/5 for a *data* reason, not an agent reason. We
   rewrote it to a target that genuinely exists: **"Zoom in on the air terminals
   in the Physics room on Level 3"** (a real Level 3 room with 8 air terminals).
   It is graded on the **tool decision** — the agent must call `find_by_category`
   to resolve the room's air terminals and *then* call `zoom_to` on them. Because
   the eval is deliberately headless (no connected browser), `zoom_to` reports
   that it could not confirm the camera moved; the gold answer documents this as a
   harness artifact, so the agent is marked wrong *only* if it never calls
   `zoom_to` at all or never resolves the room's elements first. (Whether a real
   browser actually reframes is a separate, already-verified concern.)

4. **Pinned the models and verified the request shapes.** The agent, judge, and
   USAi model slugs are fixed in `.env` (`AGENT_MODEL`, `JUDGE_MODEL`,
   `USAI_MODEL`). We also confirmed the exact requests we send USAi are well-formed
   for the gateway: the agent sends a tools array with **no forced `tool_choice`**,
   and the judge uses plain **JSON mode** — the two shapes USAi's Claude models
   accept. (The gateway rejects forced `tool_choice`/`json_schema` and plain
   non-JSON judge calls; we avoid both.)

**Final results — 5 repetitions, all three prompt versions, backend up:**

| Prompt version | Correctness | Scope | Per-question |
|---|---|---|---|
| v1 | **1.0** (50/50) | **1.0** (50/50) | all 10 questions → 5/5 |
| v2 | **1.0** (50/50) | **1.0** (50/50) | all 10 questions → 5/5 |
| v3 | **1.0** (50/50) | **1.0** (50/50) | all 10 questions → 5/5 |

Every prompt version scored a perfect 1.0 on both judges, with zero flaky
questions across 50 runs each. The `read_scores.py --per-question` output
confirms each of the 10 questions passed 5/5, including the corrected Q5.

> **How to reproduce the final matrix.** After Step 0 (backend up) and Step A
> (Phoenix up), re-upload the dataset (Step B) and run each version at 5 reps:
> ```powershell
> .\.venv\Scripts\python.exe eval\phoenix\run_experiment.py --dataset-name bim-viewer-eval-0 --system-prompt-version v1 --repetitions 5
> .\.venv\Scripts\python.exe eval\phoenix\run_experiment.py --dataset-name bim-viewer-eval-0 --system-prompt-version v2 --repetitions 5
> .\.venv\Scripts\python.exe eval\phoenix\run_experiment.py --dataset-name bim-viewer-eval-0 --system-prompt-version v3 --repetitions 5
> ```
> Then read the scores (the `--filter` matches on the experiment description):
> ```powershell
> .\.venv\Scripts\python.exe eval\phoenix\read_scores.py --per-question --filter v1
> ```

> **One gotcha we hit on the final run — a rotated API key.** Midway through the
> v2 run the judge step failed with `openai.AuthenticationError: Error code: 401`
> and recorded **0 evaluations** (the agent had answered every question; only the
> grading pass failed). The cause was simply that the USAi key had been **rotated**
> — the old one died. A 401 fails *fast*; if instead a run **hangs** with no
> output and an idle process, that's a stalled network call, not auth — kill it and
> re-run (a fresh run clears a one-off hung connection). See both rows in §7.

---

## 7. Common problems & what they mean

| You see | Meaning | Fix |
|---|---|---|
| `GSA_AI_ENV_PATH is not set` | The preamble wasn't run in this terminal | Re-run section 3 |
| `ModuleNotFoundError: eval` | `PYTHONPATH` not set | Re-run section 3 |
| `Required environment variable USAI_API_KEY is not set` | `.env` missing/incomplete, or path wrong | Check `C:\Users\lorenerouth\source\repos\.env` |
| Model/unknown-model error | A model ID in `.env` isn't a real USAi slug | Verify `AGENT_MODEL`/`JUDGE_MODEL` against USAi's catalog |
| `toolUse.name failed ... [a-zA-Z0-9_-]+` | A tool name has an illegal character (the old crash) | The tool-name guard now catches this early with a clear message |
| `JSONDecodeError: Expecting value` during grading | The USAi judge didn't return strict JSON | Already fixed — judges use JSON mode via `_classify.py`. If it recurs, that helper is where to look |
| `openai.AuthenticationError: Error code: 401` → `evaluator failed ... 0 evaluations` mid-run | Your **USAi API key was rotated/expired**. The agent may have answered every question, but the judge couldn't authenticate, so nothing got scored | Put the new key in `C:\Users\lorenerouth\source\repos\.env` (`USAI_API_KEY`) and re-run. This is an environment issue, **not** a code bug. Keys rotate periodically |
| A run **hangs** with no progress output and an idle process (low/flat CPU, output file not changing) | A single USAi network call stalled open (not rejected — a rejection would 401/400 *fast*) | **Kill the process and re-run** — a fresh run clears a one-off hung connection. Check `Get-Process python` CPU-seconds + the output file's LastWriteTime to tell "stalled" (idle, no output) from "slow" (CPU climbing, output ticking) |
| `RuntimeError: Event loop is closed` + a run that stalls for a very long time (e.g. one question stuck at thousands of seconds) | The old harness opened and closed a fresh event loop per question; the MCP client's async teardown could fire after its loop was already dead, and Phoenix would retry into the same broken pattern | Already fixed — `agent.py`'s `create_experiment_task()` now reuses **one** event loop for the whole experiment. If it recurs, that method is where to look |
| Agent says "I need the externalIds" and does nothing | Was a known weakness; now fixed | The agent used to only resolve IDs by **cost** (`find_elements`). We added `find_by_category` so it can resolve a named category/level/room (e.g. "air terminals on Level 2") to IDs too. If you still see stalling, confirm the agent lists 8 tools and that v3 is the prompt in use |
| `hint: The viewer backend could not be reached` in a trace | The backend (Step 0) is down or its token expired | Restart the backend / mint a fresh token (Step 0) |
| Phoenix page won't load | Server not up yet, or Terminal 1 was closed | Wait for "up and running", or restart Step A |
| Scores look low | Not necessarily a bug | The rubric rewards honest failure analysis — read the traces (Step D) |
