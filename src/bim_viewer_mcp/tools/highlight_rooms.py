"""Tool: highlight_rooms — draw translucent room/space volumes."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.models import sanitize_external_ids
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the highlight_rooms tool with the MCP server."""

    @mcp.tool(
        name="highlight_rooms",
        annotations={
            "title": "Highlight rooms",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def highlight_rooms(
        space_external_ids: Annotated[
            list[str],
            "Revit UniqueIds (the viewer's externalIds) of rooms/spaces to "
            "highlight. Shape: 8-4-4-4-12 hex GUID, hyphen, 8 hex digits.",
        ],
    ) -> dict[str, Any]:
        """Draw translucent volumes for a set of rooms/spaces in the viewer.

        The room volumes are added in place; the camera does NOT move. Invalid
        or duplicate ids are dropped.

        Args:
            space_external_ids: The rooms' Revit UniqueIds.

        Returns:
            {"applied": bool, "requested": int, "accepted": int}.
        """
        ids = sanitize_external_ids(space_external_ids)
        if not ids:
            return {
                "applied": False,
                "requested": len(space_external_ids or []),
                "accepted": 0,
                "hint": "Provide one or more valid Revit UniqueId externalIds.",
            }
        try:
            await post_json(
                settings.viewer_base_url,
                "api/viewer/command",
                {
                    "command": "highlight_rooms",
                    "args": {"space_external_ids": ids},
                },
            )
        except (UpstreamServiceError, ValueError):
            return {
                "applied": False,
                "requested": len(space_external_ids or []),
                "accepted": len(ids),
                "hint": "The viewer backend could not be reached.",
            }
        return {
            "applied": True,
            "requested": len(space_external_ids or []),
            "accepted": len(ids),
        }
