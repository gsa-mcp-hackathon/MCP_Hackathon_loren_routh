# Project Journey — The BIM Viewer MCP Server

*A personal record of how this project came together: the path we took, the
roadblocks we hit, the decisions that mattered, and what I learned along the
way. This is not a technical spec and not a judge-facing document — it's the
story, written for me.*

---

## What we were trying to build

The goal was a system where someone could talk to a 3D building model in plain
English — "highlight the air terminals on Level 2," "zoom in on the Physics
room," "show me the ten most expensive assets" — and have an AI agent drive the
viewer for them. Under the hood: an **MCP server** exposing a small set of
tools, an **agent** that decides which tool to call, a **backend** that queries
the building data in Databricks, and a **3D viewer** that reacts to commands.

The hackathon didn't just want the thing built — it wanted it **evaluated**:
proof that the agent reliably does the right thing, documented honestly.

---

## Phase 1 — Deciding what the tools should be

**The core idea that shaped everything:** separate *finding* from *acting*.

Early on it became clear the agent needed two different kinds of tools:

- Tools that **resolve** a plain-English description into specific element IDs
  ("which elements are air terminals on Level 2?").
- Tools that **act** on the viewer with those IDs (highlight, isolate, zoom,
  reset).

This "find, then act" split turned out to be the single most important design
decision. It kept each tool simple, made the agent's reasoning legible, and —
much later — made the evaluation easy to grade, because every question maps to a
clear tool-choice.

**The tools we landed on (the 6 the eval exercises):**

- `find_elements` — resolve a **cost** question (most expensive, over a dollar
  threshold).
- `find_by_category` — resolve a **named category** (air terminals, heat pumps,
  lighting), optionally by level and room.
- `highlight_elements`, `isolate`, `zoom_to`, `reset` — the viewer actions.

One more tool is registered in the server but untested by the gold set
(`highlight_rooms`, implemented); we chose **not** to count it in the eval, to
avoid overclaiming. `color_by` is a **planned future tool** — defined in the
tree but intentionally not registered, so it is not part of the server's tool
surface.

**Lesson:** Narrow, single-purpose tools beat one clever do-everything tool. The
breadth comes from *combining* small tools, not from making any one of them
smart. It also makes a security story possible (see Phase 4).

---

## Phase 2 — The data reality check

Building against real Databricks data forced several honest corrections to our
mental model of the building:

- Big mechanical equipment (air handlers, cooling towers, fans) lives on the
  **Roof Level** and has **no room name** — which later broke an eval question
  that assumed a "main mechanical room."
- Categories, levels, and counts are **fixed facts** we had to verify, not
  guess (e.g. 104 air terminals on Level 2; 43 heat pumps; exactly 1 cooling
  tower; 6 assets over $50k).

**A durable-ID decision that saved us pain:** we standardized on
`source_unique_id` (the Revit UniqueId) as the stable identifier the agent and
viewer share, rather than any database row ID that could shift. Everything keys
off that.

**Lesson:** Let the data correct your assumptions *early*. Almost every eval
question that later failed failed because a human (me) assumed something the data
didn't actually support.

---

## Phase 3 — The evaluation, and the roadblocks that nearly stopped it

This was the hardest, longest phase. The evaluation is what the hackathon scores
hardest, and it fought us the whole way.

**Roadblock: the eval wouldn't even run.** There was a dependency conflict
between the eval harness and the MCP server. The fix was to load the MCP tools
into the agent *in-memory* (a FastMCP client wrapping each tool), rather than
spinning up a separate process. Once that clicked, the whole harness came alive.

**Roadblock: the judge model kept rejecting our calls.** The LLM we used as the
"judge" (to grade correctness and scope) refused several standard ways of asking
for structured output. After a lot of trial and error, only one approach worked
— asking it for a plain JSON object. We stopped fighting it and built the judge
around that.

**Roadblock: the backend had to be alive, with a fresh token.** The agent's
cost/category tools call the backend, which calls Databricks. If the backend was
down or the token had expired (~1 hour), everything returned errors that *looked*
like code bugs but weren't. This cost real time until we recognized the pattern.

**Roadblock: a mid-run authentication failure.** One run died partway through.
It looked alarming, but the cause was mundane — the API key had been rotated out
from under us. The lesson stuck: a sudden 401 is almost always a stale key, not a
broken program.

**Roadblock: a hung run.** Another run just stalled. We killed it and re-ran,
and it completed cleanly in ~2.5 minutes. It was a one-off stalled network call,
not a bug — but it taught us how to tell a *hang* (kill and retry) from a *slow*
run.

**Lesson:** In an AI eval, most "failures" are environmental — expired tokens,
rotated keys, flaky network, a model that's picky about formats. Learning to
recognize the *shape* of each problem was worth more than any single fix.

---

## Phase 4 — The tough design decisions

A few calls weren't forced by bugs — they were judgment, and they shaped the
project's character.

**Security by allow-list, not cleverness.** The resolve tools accept only a
fixed list of metrics and categories — never free-form SQL or arbitrary field
names. This deliberately makes `find_elements` "cost-only" and `find_by_category`
"category-only." It feels narrow, but the narrowness *is* the security control,
and it became a genuine strength to point to.

**The Q5 honesty call.** One gold question originally asked the agent to zoom to
the "main mechanical room" — a room that, per the data, doesn't exist (that
equipment is on the Roof Level with no room name). We caught it, and instead of
quietly swapping in an easy question, we **rewrote Q5 and documented the
correction openly** in the methodology. Being honest about a mistake we found
ourselves turned out to be exactly the kind of thing a rigorous eval rewards.

**Grading the decision, not the pixels.** Because the eval runs headless (no real
browser), `zoom_to` can't actually prove the camera moved. We decided to grade
the **tool decision** (did the agent choose to find-then-zoom correctly?) and
document the headless limitation plainly, rather than pretend we verified a
visual result we couldn't see.

**The spatial boundary we chose to name.** Late in the review, a simple question
— "which heat pump is the northern-most on Level 2?" — exposed that our tools
return no geometry at all (no coordinates, no orientation). So "northern-most,"
"nearest," "largest" are genuinely unanswerable by design. Rather than hide this,
we wrote it into the documentation as a known, deliberate limitation.

**Lesson:** The decisions I'm proudest of are the ones where we chose to *name a
weakness* instead of papering over it. An honest limitation reads as maturity;
a hidden one reads as a landmine.

---

## Phase 5 — Where it landed

After all of that, the results came out clean:

- Three prompt versions (v1/v2/v3) all scored **perfect correctness and scope**,
  with every gold question passing consistently across repeated trials.
- A **no-tools baseline** (the agent with its tools taken away) scored **0.70** —
  giving us a clean, defensible "tool lift" of **+0.30**. That number is the
  honest proof that the tools actually matter.
- The `find_by_category` tool and the "find, then act" prompt (v3) were direct
  responses to an early failure where the agent could only find things by cost
  and stalled on "find the air terminals." Watching the baseline confirmed those
  were the exact questions that were flaky without a resolution tool.

---

## The biggest lessons, distilled

1. **Small, single-purpose tools win.** Breadth comes from composition, and it
   makes both reasoning and security tractable.
2. **Let the data correct you early.** Most of our eval failures traced back to a
   human assumption the data never supported.
3. **Most AI-eval "bugs" are environmental.** Tokens, keys, network, picky model
   formats. Learn the shape of each.
4. **Name your limitations.** The spatial gap, the headless scoring, the Q5
   correction, the prompt-injection gap — stating them plainly made the work
   stronger, not weaker.
5. **A perfect score is a signal to raise the bar, not to stop.** An eval that
   always passes has stopped measuring.

---

## What's still ahead (as of this writing)

- Finish the section-by-section review of the evaluation write-up.
- A clean end-to-end **demo** against a real browser (including the camera move
  the eval runs headless).
- The **deck**, and submission ahead of the deadline.
- Afterward: a separate **capabilities/overview** document (who it's for, what it
  can and can't do, roadmap), with the deep technical backlog staying in
  `FUTURE_IMPROVEMENTS.md`.

---

*Last updated during the §1 review of the evaluation write-up.*
