"""Tool: reset — clear highlights, rooms, and focus; show the whole building."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the reset tool with the MCP server."""

    @mcp.tool(
        name="reset",
        annotations={
            "title": "Reset view",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def reset() -> dict[str, Any]:
        """Reset the viewer to a clean state showing the whole building.

        Clears any element highlights, room volumes, and camera focus. Safe to
        call at any time; idempotent.

        Returns:
            {"applied": bool}.
        """
        try:
            await post_json(
                settings.viewer_base_url,
                "api/viewer/command",
                {"command": "reset", "args": {}},
            )
        except (UpstreamServiceError, ValueError):
            return {
                "applied": False,
                "hint": "The viewer backend could not be reached.",
            }
        return {"applied": True}
