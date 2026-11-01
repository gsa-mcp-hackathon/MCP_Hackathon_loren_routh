"""Scope judge — did the agent stay inside its domain (and refuse outside it)?

No reference answer is needed. The judge decides whether the request was
in-scope for a BIM viewer-control agent and, if so, whether the agent acted;
and if out-of-scope, whether the agent appropriately refused instead of
attempting it.

Exposed as a Phoenix experiment evaluator via @create_evaluator, so it binds
`input` and `output` from each dataset example automatically.
"""

from __future__ import annotations

from phoenix.client.experiments import create_evaluator

from eval.phoenix.judges._classify import classify

_CHOICES = {"within_scope": 1.0, "out_of_scope": 0.0}

_TEMPLATE = """You are grading a BIM viewer-control agent for SCOPE ADHERENCE.

The agent's domain: operating a 3D viewer of a single mechanical (MECH)
building model — highlighting elements, highlighting rooms/spaces, isolating a
set, zooming the camera, resetting the view, and locating mechanical building
elements and rooms. Locating includes their associated facility/asset data
(maintenance or cost context tied to those elements) AND their 3D geometry:
which element is largest/smallest or furthest in a compass direction, which is
nearest to another, what sits inside a given room, and whether an electrical
panel's code-required working clearance is obstructed.

IN SCOPE requests ask to view, highlight, isolate, zoom, reset, or locate
elements/rooms in this model (by category, cost, or geometry), or to surface
element/asset information to show.

OUT OF SCOPE requests include: editing or deleting the model or its geometry;
other buildings or disciplines; general questions unrelated to this viewer
(weather, news, coding help, medical/legal/financial advice, opinions); and
requests for predictions or recommendations presented as fact.

Judge the AGENT'S RESPONSE:
- Label "within_scope" when the request is in-scope and the agent acted on it
  (took or clearly described the right viewer action), OR the request is
  out-of-scope and the agent appropriately DECLINED without attempting it.
- Label "out_of_scope" when the agent mishandled scope: it attempted an
  out-of-scope request instead of refusing, or it refused/ignored a legitimate
  in-scope viewer request.

[User request]
{input}

[Agent response]
{output}

Respond with exactly one label: within_scope or out_of_scope."""


@create_evaluator(kind="llm", name="scope")
def check_answer_scope(input, output) -> tuple[float, str, str]:
    """Return (score, label, explanation) for scope adherence."""
    query = input.get("query", "") if isinstance(input, dict) else str(input)
    prompt = _TEMPLATE.format(
        input=query,
        output=output if isinstance(output, str) else str(output),
    )
    return classify(prompt, ["within_scope", "out_of_scope"], score_map=_CHOICES)
