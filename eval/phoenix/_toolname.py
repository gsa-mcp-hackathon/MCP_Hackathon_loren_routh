"""Tool-name validation for Claude-backed model calls.

Claude (Anthropic/Bedrock Converse, used by USAi) requires every tool name to
match [a-zA-Z0-9_-]+. A single illegal name makes the API reject the ENTIRE
messages array. The agent-under-test runs on Claude (e.g. claude_4_5_sonnet),
so we validate names up front — one bad name fails loudly here instead of as a
cryptic rejection deep in a Phoenix run.
"""
from __future__ import annotations

import re

_VALID = re.compile(r"^[a-zA-Z0-9_-]+$")


def is_valid_tool_name(name: str) -> bool:
    return bool(_VALID.match(name))


def assert_tool_names_valid(names: list[str]) -> None:
    """Raise if any tool name violates Claude's [a-zA-Z0-9_-]+ rule."""
    bad = [n for n in names if not is_valid_tool_name(n)]
    if bad:
        raise ValueError(
            "Tool name(s) violate Claude's [a-zA-Z0-9_-]+ rule and would be "
            f"rejected by the model: {bad}"
        )
