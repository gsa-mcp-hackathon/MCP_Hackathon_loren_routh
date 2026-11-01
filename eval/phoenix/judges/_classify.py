"""Shared LLM-as-judge classifier for the USAi gateway.

Why this exists
---------------
Phoenix's built-in ``ClassificationEvaluator`` asks the model for a label two
ways: OpenAI *structured outputs* (``response_format={"type": "json_schema"}``)
and, as a fallback, *tool calling* with a forced ``tool_choice``. The USAi
gateway's Claude models do not honor either path the way Phoenix needs:

* With ``json_schema`` strict mode, the model ignores the schema and returns
  prose (e.g. ``**Correct.** 2 + 2 = 4.``), so ``json.loads`` fails with
  ``JSONDecodeError: Expecting value``.
* With forced tool calling, the gateway rejects the request:
  ``tool_choice: type "tool" and "any" are not supported for this model``.

Both leave every example ungraded (``0 evaluations``).

What works
----------
Plain ``response_format={"type": "json_object"}`` (JSON mode) is honored by the
USAi Claude models and reliably returns a parseable JSON object. This helper
uses that mode directly, keeping each judge's existing prompt template, label
set, and score mapping unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Sequence

from openai import OpenAI

from eval.phoenix import _env

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _env.load_eval_env()
        _client = OpenAI(
            api_key=_env.require("USAI_API_KEY"),
            base_url=_env.usai_api_base(),
        )
    return _client


def classify(
    prompt: str,
    labels: Sequence[str],
    *,
    score_map: dict[str, float],
) -> tuple[float, str, str]:
    """Run one classification and return ``(score, label, explanation)``.

    ``prompt`` is the fully rendered judge template. ``labels`` is the ordered
    list of valid labels. ``score_map`` maps each label to its numeric score.
    The model is asked, via JSON mode, to return ``{"label", "explanation"}``;
    the label is validated against ``labels``.
    """
    label_list = ", ".join(labels)
    instruction = (
        f"{prompt}\n\n"
        "Respond ONLY with a single JSON object and nothing else — no prose, "
        "no markdown, no code fences. The object must have exactly two keys:\n"
        f'  "label": one of [{label_list}]\n'
        '  "explanation": a brief string explaining your reasoning\n'
        f'Example: {{"label": "{labels[0]}", "explanation": "..."}}'
    )

    client = _get_client()
    response = client.chat.completions.create(
        model=_env.judge_model(),
        messages=[{"role": "user", "content": instruction}],
        response_format={"type": "json_object"},
    )
    content = (response.choices[0].message.content or "").strip()

    obj = _parse_json_object(content)
    if obj is None:
        raise ValueError(
            f"Judge did not return parseable JSON. Raw response: {content!r}"
        )

    label = str(obj.get("label", "")).strip()
    explanation = str(obj.get("explanation", "")).strip()

    if label not in score_map:
        raise ValueError(
            f"Judge returned invalid label {label!r}; "
            f"valid labels are {list(score_map)}. Raw response: {content!r}"
        )

    return (score_map[label], label, explanation)


def _parse_json_object(content: str) -> dict | None:
    """Return the JSON object in ``content``, tolerating leading/trailing prose.

    We request JSON mode, but the USAi Claude gateway occasionally wraps the
    object in reasoning prose (e.g. "I judge it correct.\n\n{...}"). First try a
    strict parse; if that fails, extract the LAST balanced ``{...}`` span (the
    final object is the judge's actual verdict) and parse that.
    """
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    for match in reversed(list(re.finditer(r"\{", content))):
        start = match.start()
        depth = 0
        for i in range(start, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(content[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
    return None
