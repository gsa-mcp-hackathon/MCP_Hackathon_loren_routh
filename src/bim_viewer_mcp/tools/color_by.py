"""Tool: color_by — a planned future tool (defined, not yet registered).

This module is kept in the tree as a design placeholder for a future pass, but
it is intentionally NOT registered by `tools/__init__.py`: the server exposes
only tools that actually do something, so a client's discovered tool list never
includes a not-implemented stub. `register(mcp)` is retained so the feature can
be turned on in one line once it is built.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the color_by tool with the MCP server."""

    @mcp.tool(
        name="color_by",
        annotations={
            "title": "Color by",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def color_by(
        field: Annotated[
            str,
            "The field to color elements by (e.g. a category or level). "
            "Reserved — not yet implemented.",
        ],
    ) -> dict[str, Any]:
        """Color elements by a field value (NOT YET IMPLEMENTED).

        Reserved for a future pass. The call is accepted and forwarded, but the
        viewer currently returns "not implemented" and makes no change.

        Args:
            field: The field to color by.

        Returns:
            {"applied": False, "reason": "not_implemented"}.
        """
        if not isinstance(field, str) or not field.strip():
            return {"applied": False, "reason": "not_implemented"}
        try:
            await post_json(
                settings.viewer_base_url,
                "api/viewer/command",
                {"command": "color_by", "args": {"field": field.strip()[:64]}},
            )
        except (UpstreamServiceError, ValueError):
            return {"applied": False, "reason": "not_implemented"}
        return {"applied": False, "reason": "not_implemented"}
