"""Tool: find_by_category — resolve a category (+ level/room) to externalIds.

Like find_elements, this tool RETURNS data rather than performing a viewer
action: it answers "which elements are in this category?" by asking the viewer
backend, which queries the facility/asset data and joins it to the durable
Revit UniqueId (= the viewer's externalId). The agent then feeds those ids to
highlight_elements, isolate, or zoom_to.

Only an allowlisted category and level are accepted — never a raw asset_type,
free-form field, or SQL. The backend maps each category to its asset_type set
server-side and enforces the same allowlists (defense in depth). The viewer
backend origin is fixed by server config, never a tool argument.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.models import (
    ALLOWED_CATEGORIES,
    ALLOWED_LEVELS,
    MAX_CATEGORY_LIMIT,
    valid_external_id,
)
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the find_by_category tool with the MCP server."""

    @mcp.tool(
        name="find_by_category",
        annotations={
            "title": "Find elements by category",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def find_by_category(
        category: Annotated[
            Literal[
                "air_terminals",
                "supply_diffusers",
                "return_grilles",
                "exhaust_grilles",
                "heat_pumps",
                "cooling_tower",
                "air_handlers",
                "fans",
                "transformers",
                "electrical_panels",
                "water_heaters",
                "sprinklers",
                "lighting",
                "receptacles",
                "plumbing_fixtures",
            ],
            "The element category to resolve. 'air_terminals' covers supply "
            "diffusers, return grilles, and exhaust grilles; 'heat_pumps' is "
            "the water-source heat pumps (WSHP).",
        ],
        level: Annotated[
            Literal["Level 1", "Level 2", "Level 3", "Roof Level"] | None,
            "Optional building level to restrict to. Omit for all levels.",
        ] = None,
        room_name: Annotated[
            str | None,
            "Optional case-insensitive room-name substring to restrict to "
            "(e.g. 'mechanical', 'chemistry'). Omit for all rooms.",
        ] = None,
        limit: Annotated[
            int,
            "Max elements to return (1-5000).",
        ] = 500,
    ) -> dict[str, Any]:
        """Find building elements by category; return their externalIds.

        Use this to answer category/level/room questions (e.g. "air terminals
        on Level 2", "the cooling tower", "the water-source heat pumps"), then
        pass the returned external_ids to highlight_elements (to show them in
        place) or isolate. This tool does NOT change the view by itself.

        Examples:
            - Air terminals on Level 2: category='air_terminals', level='Level 2'
            - The cooling tower:        category='cooling_tower'
            - Heat pumps:               category='heat_pumps'

        Returns:
            {"category": str, "count": int,
             "elements": [{external_id, asset_type, description, level_name,
             room_name}]}.
        """
        if category not in ALLOWED_CATEGORIES:
            return {
                "category": category,
                "count": 0,
                "elements": [],
                "hint": "Unsupported category.",
            }
        if level is not None and level not in ALLOWED_LEVELS:
            return {
                "category": category,
                "count": 0,
                "elements": [],
                "hint": "Unsupported level.",
            }
        bounded_limit = max(1, min(int(limit or 500), MAX_CATEGORY_LIMIT))
        payload: dict[str, Any] = {
            "category": category,
            "limit": bounded_limit,
        }
        if level is not None:
            payload["level"] = level
        if room_name:
            payload["room_name"] = room_name
        try:
            data = await post_json(
                settings.viewer_base_url,
                "api/assets/by-category",
                payload,
            )
        except (UpstreamServiceError, ValueError):
            return {
                "category": category,
                "count": 0,
                "elements": [],
                "hint": "The viewer backend could not be reached.",
            }

        raw = data.get("elements", []) if isinstance(data, dict) else []
        elements: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            ext = row.get("external_id")
            if not valid_external_id(ext):
                continue
            elements.append(
                {
                    "external_id": ext,
                    "asset_type": row.get("asset_type"),
                    "description": row.get("description"),
                    "level_name": row.get("level_name"),
                    "room_name": row.get("room_name"),
                }
            )
        return {
            "category": category,
            "count": len(elements),
            "elements": elements,
        }
