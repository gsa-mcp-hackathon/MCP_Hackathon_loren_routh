"""Correctness judge — did the agent pick the right tool with the right intent?

Our MCP tools perform ACTIONS (highlight, isolate, zoom, reset) rather than
return facts, so "correctness" is not string-matching a factual answer. It is:
did the agent choose the tool and the target set that the request calls for?

The `expected` reference describes the correct behavior in words (which tool,
on what elements/rooms, and whether the camera should move). The judge compares
the agent's described action to that reference, tolerating phrasing, formatting,
and the presence or absence of concrete ids (the agent may not have been handed
specific ids and may describe the set instead).

Exposed as a Phoenix experiment evaluator via @create_evaluator, so it binds
`input`, `output`, and `expected` from each dataset example automatically.
"""

from __future__ import annotations

from phoenix.client.experiments import create_evaluator

from eval.phoenix.judges._classify import classify

_CHOICES = {"correct": 1.0, "incorrect": 0.0}

_TEMPLATE = """You are grading a BIM viewer-control agent.

The agent operates a 3D viewer of a single mechanical (MECH) building model. It
can only call these tools:
  - resolve tools (return which elements a request is about, no view change):
    find_elements (by cost), find_by_category (by named category/level/room),
    find_nearest (closest to a reference element by true 3D distance),
    find_extreme (largest/smallest by 3D volume, or northmost/southmost/
    eastmost/westmost), find_in_room (assets inside a room's 3D volume),
    find_clearance (checks an electrical panel's NEC 110.26 working clearance).
  - action tools: highlight_elements, highlight_rooms, isolate, zoom_to, reset.
A plain highlight or room display must NOT move the camera; only zoom_to moves
the camera.

You are judging CORRECTNESS: did the agent take the action the request calls
for? That means choosing the RIGHT TOOL(S) on the RIGHT target set, and honoring
the camera rule. Many requests need a resolve tool FIRST (to get the elements)
and then an action tool. For geometry requests ("largest", "furthest north",
"nearest to X", "in room N", "is the clearance clear"), the right resolve tool
is the matching geometry tool, not a cost or plain-category guess. The agent may
describe the target set in words rather than listing concrete ids — that is
acceptable. Ignore cosmetic differences in wording, formatting, or surrounding
prose. Do not require exact id lists or numeric coordinates.

Mark "correct" when the agent's action matches the expected action: same tool
chain (or an equivalently correct tool for the intent), same target, and the
camera moved only if the expected action moves it.

Mark "incorrect" when the agent chose the wrong tool, targeted the wrong set,
moved the camera when it should not have (or vice versa), invented ids or
measurements, or failed to act when it should have.

[User request]
{input}

[Expected action]
{expected}

[Agent response]
{output}

Respond with exactly one label: correct or incorrect."""


@create_evaluator(kind="llm", name="correctness")
def match_expected_response(input, output, expected) -> tuple[float, str, str]:
    """Return (score, label, explanation) for the agent's tool choice."""
    query = input.get("query", "") if isinstance(input, dict) else str(input)
    reference = ""
    if isinstance(expected, dict):
        reference = expected.get("responses", "") or expected.get(
            "reference", ""
        )
    elif expected is not None:
        reference = str(expected)

    prompt = _TEMPLATE.format(
        input=query,
        expected=reference,
        output=output if isinstance(output, str) else str(output),
    )
    return classify(prompt, ["correct", "incorrect"], score_map=_CHOICES)
    scores = evaluator.evaluate(
        {
            "input": query,
            "output": output if isinstance(output, str) else str(output),
            "expected": reference,
        }
    )
    score = scores[0]
    numeric = score.score if score.score is not None else 0.0
    return (numeric, score.label or "", score.explanation or "")
