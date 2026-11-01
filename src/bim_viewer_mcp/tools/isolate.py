"""Tool: isolate — show only a set of elements, hiding everything else."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.models import sanitize_external_ids
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the isolate tool with the MCP server."""

    @mcp.tool(
        name="isolate",
        annotations={
            "title": "Isolate elements",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def isolate(
        external_ids: Annotated[
            list[str],
            "Revit UniqueIds (the viewer's externalIds) of elements to isolate. "
            "Shape: 8-4-4-4-12 hex GUID, hyphen, 8 hex digits.",
        ],
    ) -> dict[str, Any]:
        """Isolate a set of elements: show only them and hide the rest.

        A stronger focus than highlight_elements — everything outside the set is
        hidden. The camera does NOT move. Call reset to restore the full model.

        Args:
            external_ids: The elements' Revit UniqueIds.

        Returns:
            {"applied": bool, "requested": int, "accepted": int}.
        """
        ids = sanitize_external_ids(external_ids)
        if not ids:
            return {
                "applied": False,
                "requested": len(external_ids or []),
                "accepted": 0,
                "hint": "Provide one or more valid Revit UniqueId externalIds.",
            }
        try:
            await post_json(
                settings.viewer_base_url,
                "api/viewer/command",
                {"command": "isolate", "args": {"external_ids": ids}},
            )
        except (UpstreamServiceError, ValueError):
            return {
                "applied": False,
                "requested": len(external_ids or []),
                "accepted": len(ids),
                "hint": "The viewer backend could not be reached.",
            }
        return {
            "applied": True,
            "requested": len(external_ids or []),
            "accepted": len(ids),
        }
