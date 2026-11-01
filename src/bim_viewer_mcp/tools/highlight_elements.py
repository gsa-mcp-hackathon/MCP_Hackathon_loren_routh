"""Tool: highlight_elements — color a set of elements in place.

One-tool-per-file pattern: this module exposes a single `register(mcp)` that
attaches one async `@mcp.tool`. The tool validates its arguments (every id is
treated as untrusted), then POSTs one allowlisted command to the viewer backend,
which broadcasts it to every connected 3D viewer.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.models import sanitize_external_ids
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the highlight_elements tool with the MCP server."""

    @mcp.tool(
        name="highlight_elements",
        annotations={
            "title": "Highlight elements",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def highlight_elements(
        external_ids: Annotated[
            list[str],
            "Revit UniqueIds (the viewer's externalIds) of elements to "
            "highlight. Shape: 8-4-4-4-12 hex GUID, hyphen, 8 hex digits.",
        ],
    ) -> dict[str, Any]:
        """Highlight a set of building elements in the 3D viewer, in place.

        The elements are colored where they stand; the camera does NOT move.
        Invalid or duplicate ids are dropped. Use this to show an answer
        visually; use zoom_to afterward only if the user asks to zoom.

        Args:
            external_ids: The elements' Revit UniqueIds.

        Returns:
            {"applied": bool, "requested": int, "accepted": int} — small,
            non-sensitive summary. No ids are echoed back.
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
                {"command": "highlight_elements", "args": {"external_ids": ids}},
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
