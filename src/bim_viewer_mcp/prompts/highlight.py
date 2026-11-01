"""A conversation-starter prompt for driving the BIM viewer."""

from __future__ import annotations

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register the viewer prompt with the MCP server."""

    @mcp.prompt()
    def highlight_in_viewer(subject: str) -> str:
        """Start a request to find and highlight something in the 3D viewer.

        Args:
            subject: What to find — e.g. "all air terminals on Level 2".
        """
        return (
            f"Find {subject} in the building model and highlight them in the 3D "
            "viewer. Highlight in place without moving the camera. If I ask you "
            "to zoom in afterward, use zoom_to."
        )
