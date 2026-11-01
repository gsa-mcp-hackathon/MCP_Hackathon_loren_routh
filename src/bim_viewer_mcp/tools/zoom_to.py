"""Tool: zoom_to — move the camera to frame a room or a set of elements.

This is the ONLY tool that moves the camera. A plain highlight never does.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.models import sanitize_external_ids, valid_external_id
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the zoom_to tool with the MCP server."""

    @mcp.tool(
        name="zoom_to",
        annotations={
            "title": "Zoom to",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def zoom_to(
        external_ids: Annotated[
            list[str] | None,
            "Revit UniqueIds of elements to frame. Ignored if "
            "space_external_id is given.",
        ] = None,
        space_external_id: Annotated[
            str | None,
            "Revit UniqueId of a single room/space to frame. Takes precedence "
            "over external_ids. The room must already be highlighted.",
        ] = None,
    ) -> dict[str, Any]:
        """Move the camera to frame a room or a set of elements.

        This is the only tool that changes the view — use it only when the user
        asks to zoom in on something. Provide a room via space_external_id, or
        a set of elements via external_ids. A room takes precedence if both are
        given. To frame a room it must already be highlighted (highlight_rooms).

        Args:
            external_ids: Elements to frame.
            space_external_id: A single room to frame (takes precedence).

        Returns:
            {"applied": bool, "target": "room"|"elements"|None}.
        """
        if valid_external_id(space_external_id):
            args = {"space_external_id": space_external_id}
            target = "room"
        else:
            ids = sanitize_external_ids(external_ids)
            if not ids:
                return {
                    "applied": False,
                    "target": None,
                    "hint": (
                        "Provide a valid room space_external_id or one or more "
                        "element external_ids."
                    ),
                }
            args = {"external_ids": ids}
            target = "elements"

        try:
            result = await post_json(
                settings.viewer_base_url,
                "api/viewer/command",
                {"command": "zoom_to", "args": args},
            )
        except (UpstreamServiceError, ValueError):
            return {
                "applied": False,
                "target": target,
                "hint": "The viewer backend could not be reached.",
            }
        applied = bool(result.get("applied")) if isinstance(result, dict) else True
        return {"applied": applied, "target": target if applied else None}
