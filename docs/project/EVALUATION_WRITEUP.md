# Evaluation Document — BIM Viewer MCP Server

**GSA MCP and AI Agent Hackathon**

| | |
|---|---|
| **Server under test** | BIM Viewer MCP Server (`bim_viewer_mcp`) |
| **Evaluation framework** | Arize Phoenix (experiments + OpenTelemetry tracing) |
| **Agent model** | `claude_4_5_sonnet` (temperature 0) |
| **Judge model** | `claude-sonnet-5.5` (temperature 0, JSON mode) |
| **Dataset** | `bim-viewer-eval-0` (16 gold questions) |
| **Date** | 2026-10-30 |

## 1. Overview and Objectives

This evaluation measures whether the BIM Viewer MCP server improves an agent's
ability to drive a 3D building model correctly and within scope: picking the right
tool on the right elements, moving the camera only when asked, and declining
requests outside its authority (destructive edits, purchasing advice, unrelated
topics).

The eval exercises **ten tools** over a single mechanical (MECH) model, in two
groups — **resolve** (find which elements a request is about) and **act** (drive
the viewer):

| Tool | Group | What it does |
|---|---|---|
| `find_elements` | resolve | Select elements by **cost only** — top-N or a dollar threshold on `replacement_cost`/`maintenance_cost`. |
| `find_by_category` | resolve | Select by **named category** (e.g. air terminals, heat pumps), optionally by level and room. |
| `find_extreme` | resolve | Rank elements by **true 3D geometry** — largest/smallest by bounding-box volume, or northmost/southmost/eastmost/westmost. |
| `find_nearest` | resolve | Rank elements by **true 3D distance** to a reference element (closest-first). |
| `find_in_room` | resolve | Select the assets whose **3D center falls inside** a room's volume (by room number or id). |
| `find_clearance` | resolve | Check an electrical panel's **NEC 110.26** working-space clearance in 3D and report whether anything intrudes. |
| `highlight_elements` | act | Color a set of elements in place, no camera move. |
| `isolate` | act | Show only a set and hide everything else. |
| `zoom_to` | act | Move the camera to frame a room or set. |
| `reset` | act | Clear highlights/isolation; show the whole building. |

The pattern is **find, then act**. The four geometry resolve tools compute real
bounding-box math server-side in Python, so spatial questions ("northern-most,"
"nearest," "largest," "what's in this room," "is this clearance clear") are now
first-class: resolve the element(s) by geometry, then highlight / isolate / zoom.
One registered tool is not exercised here and is excluded from all counts:
`highlight_rooms` (implemented). `color_by` is a **planned future tool** —
defined in the tree but intentionally not registered, so it is not part of the
server's tool surface.

## 2. Testing Methodology

The eval runs on **Arize Phoenix**. Gold questions live in one source of truth
(`evaluation.xml`) compiled to CSV; system prompts are versioned (`v1`–`v3`); and
OTLP tracing captures every tool call, argument, and latency so each verdict can
be audited. It scores **tool choice, not pixels**, and runs **headless** (no
browser) for reproducibility; the browser repaint is covered by the viewer's own
tests.

**Golden dataset (`bim-viewer-eval-0`, 16 questions)** — chosen to span the
decisions that matter, not just easy lookups. Items 1–10 cover category/cost
resolves, actions, and refusals; items 11–16 cover the true-3D geometry tools:

| # | What it tests | Expected action |
|---|---|---|
| 1 | Category resolve + highlight | `find_by_category` → `highlight_elements` |
| 2 | Cost ranking | `find_elements` (top 10 replacement cost) → highlight |
| 3 | Cost threshold | `find_elements` (> $50k) → highlight |
| 4 | Isolate vs. highlight | `isolate` the cooling tower |
| 5 | Camera move | `find_by_category` → `zoom_to` |
| 6 | Maintenance-cost ranking | `find_elements` → highlight |
| 7 | Reset the view | `reset` |
| 8 | **Refuse** a destructive edit | decline, no tool |
| 9 | **Refuse** out-of-domain purchasing advice | decline, no tool |
| 10 | **Mixed** — do in-scope part, refuse rest | highlight heat pumps, decline weather |
| 11 | Largest by 3D volume | `find_extreme` (largest) → highlight |
| 12 | Directional extreme | `find_extreme` (northmost air handler) → highlight |
| 13 | Nearest to a reference | resolve cooling tower → `find_nearest` (transformers) → highlight |
| 14 | Room contents (3D center-in-volume) | `find_in_room` (room 54) → highlight |
| 15 | NEC clearance check | `find_clearance` on a panel → report obstructed |
| 16 | Smallest by 3D volume + isolate | `find_extreme` (smallest transformer) → `isolate` |

**Two LLM judges** (temperature 0) score each response along the two failure modes
that matter — acting wrongly and overreaching:

| Judge | Dimension | Labels (score) |
|---|---|---|
| `match_expected_response` | **Correctness** — right tool, right targets, camera only when asked | `correct` (1) / `incorrect` (0) |
| `check_answer_scope` | **Scope** — answers in-domain, refuses out-of-domain | `within_scope` (1) / `out_of_scope` (0) |

A model judge is used rather than string matching because the agent's wording
varies ("highlight the Level 2 air terminals" vs. "I'll highlight all air
terminals on Level 2"). For the headline consistency run each question is run
**3 times**, so a score is a stability signal (3/3, 1/3, …), not a single lucky
pass.

## 3. Performance Metrics and Results

**With tools**, the agent scored a **perfect 1.0** on both judges with zero flaky
questions. The headline run repeated all 16 questions **3 times** (48 agent runs,
96 evaluations): every question passed **3/3** on correctness and **3/3** on scope
— including all six geometry items (largest, northmost air handler, nearest
transformer, room-54 contents, panel clearance, smallest transformer).

Re-running with the tools hidden (`--no-tools`) isolates the server's
contribution:

| Question group | No tools | With tools |
|---|---|---|
| Items 1, 2, 7, 8, 9, 10, 12, 14 (categories, cost ranking, actions, refusals, northmost, room contents) | pass | pass |
| Item 3 — replacement cost over $50k | pass | pass |
| Item 5 — zoom to Physics-room air terminals | **fail** | pass |
| Item 11 — largest by 3D volume | **fail** | pass |
| Item 13 — nearest transformer to the cooling tower | **fail** | pass |
| Item 15 — NEC clearance check on a panel | **fail** | pass |
| Item 16 — isolate the smallest transformer | **fail** | pass |
| **Overall correctness** | **0.69** (11/16) | **1.00** (16/16) |

**Tool lift: +0.31 correctness.** Scope stayed **1.0 in both** configurations:
tools buy correctness of action, not better judgment about what to refuse. The
failures without tools are exactly the requests that need data or geometry the
model cannot invent — ranking by cost it cannot see, and the four true-3D queries
(largest, nearest, smallest, clearance). A full 3-rep consistency run (48 agent
runs + 96 evaluations) completes in about 11 minutes; the single-rep no-tools
baseline runs in a few minutes.

**Failure analysis.** Earlier runs surfaced two real problems, both fixed. (a) The
agent could only resolve by cost or category, so geometry requests ("largest,"
"nearest," "smallest," "is this clearance clear") had no tool to call — fixed by
adding the four true-3D resolve tools (`find_extreme`, `find_nearest`,
`find_in_room`, `find_clearance`) and extending the prompt (`v3`) to route
geometry questions to them. (b) The original Q5 targeted a "main mechanical room"
that does not exist in the data (that equipment sits on the Roof Level with no
room name); repointed to the Physics room air terminals on Level 3, confirmed
against the backend.

## 4. Security Considerations

**Data classification.** A single synthetic/demo MECH model with synthetic Maximo
cost data — **no PII or CUI** in the model, queries, dataset, or traces. A
production BIM deployment could expose facility or security information and would
warrant a stricter classification.

**Scope as a control.** The `check_answer_scope` judge is a guardrail: it verifies
the agent refuses out-of-authority requests — deleting the model (Q8), purchasing
advice (Q9), unrelated topics (Q10). Scope scored **1.0 across every run**,
including with tools disabled.

**Tool-surface controls.** The surface is **read/view-only by construction** — no
tool edits, writes, or deletes the model. Viewer commands are constrained by an
explicit allow-list (`ALLOWED_COMMANDS`); all arguments and outputs are untrusted,
with ID-shape validation and length caps; tools cannot execute code, navigate,
fetch, or read file paths, and the backend origin is fixed by server config (no
tool-supplied-URL or DNS-rebinding surface). Prompt-injection and adversarial-input
testing would be added for production.

**Secrets and transport.** Credentials are never committed — the USAi key loads
from a `.env` outside the project (`GSA_AI_ENV_PATH`) and the Databricks token is
minted fresh at startup. The whole loop stays local (Phoenix on `localhost:6006`,
backend on `127.0.0.1:8000`).

## 5. Lessons Learned and Recommendations

**What worked.** Versioned prompts + one compiled dataset made runs reproducible
and A/B-comparable; LLM-as-judge absorbed natural-language variance an exact-match
grader would fail on; repeating each question across reps exposed the no-tools
flakiness a single run would hide; and the no-tools baseline gave a defensible
tool-lift number.

**Limitations.** Small dataset (16 questions) — enough to span the decision types,
not for statistical confidence. The judge is itself an LLM (failing cases were
human-reviewed). Headless scoring verifies the `zoom_to` decision but not the
browser reframe. The geometry tools reason over Revit **bounding boxes**, not tight
meshes, so a loose box can over- or under-state a true distance; the clearance
check currently excludes lighting fixtures from the intrusion test for that reason
(a known, documented limitation pending tight mesh geometry).

**Recommendations.**

1. **Grow and stratify the gold set** — multi-step requests, ambiguous targets,
   more rooms/levels, more geometry edge cases — until the agent fails again.
2. **Tighten the geometry inputs.** The resolve tools now expose position, size,
   nearness, room membership, and NEC clearance, computed in true 3D — the whole
   reason for a 3D model and viewer. The next step is replacing loose Revit
   bounding boxes with tight mesh geometry so distance and clearance checks need no
   category exclusions.
3. **Add adversarial and prompt-injection test cases** for any move to production.
4. **Keep the no-tools baseline** each reporting cycle so the tool-lift claim stays
   honest as the dataset grows.
