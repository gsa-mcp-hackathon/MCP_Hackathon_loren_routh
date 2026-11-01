"""Tool: find_nearest — the elements physically closest to a reference element.

Like find_elements and find_by_category, this tool RETURNS data rather than
performing a viewer action: it answers "what is nearest to this element?" by
asking the viewer backend, which ranks candidates by TRUE 3D distance — the gap
between axis-aligned bounding boxes, which is 0 when they overlap or touch. This
is real 3D math (done server-side in Python), not a 2D footprint distance, so
two elements stacked on different floors are correctly far apart.

The reference is addressed by its durable Revit UniqueId (= the viewer's
externalId), and an optional category restricts what counts as a neighbor. No
free-form field, coordinate, or SQL is ever passed through; the backend enforces
the same allowlists (defense in depth). The viewer backend origin is fixed by
server config, never a tool argument.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.models import (
    ALLOWED_CATEGORIES,
    ALLOWED_LEVELS,
    MAX_SPATIAL_LIMIT,
    valid_external_id,
)
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the find_nearest tool with the MCP server."""

    @mcp.tool(
        name="find_nearest",
        annotations={
            "title": "Find the nearest elements to a reference element",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def find_nearest(
        reference_external_id: Annotated[
            str,
            "The externalId of the element to measure from (e.g. the panel or "
            "AHU the question is about). Get it from find_by_category or "
            "find_elements first.",
        ],
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
            ]
            | None,
            "Optional category to restrict the neighbors to (e.g. the nearest "
            "transformer). Omit to consider all tracked assets.",
        ] = None,
        level: Annotated[
            Literal["Level 1", "Level 2", "Level 3", "Roof Level"] | None,
            "Optional building level to restrict the neighbors to. Omit for "
            "all levels.",
        ] = None,
        limit: Annotated[
            int,
            "How many of the closest elements to return (1-50).",
        ] = 5,
    ) -> dict[str, Any]:
        """Find the elements physically closest to a reference element.

        Use this to answer proximity questions (e.g. "what's the nearest
        transformer to this panel?", "which air terminals are closest to the
        cooling tower?"), then pass the returned external_ids to
        highlight_elements or zoom_to. This tool does NOT change the view by
        itself.

        Distance is the true 3D gap between bounding boxes in feet: 0 means the
        elements overlap or touch, and elements on different floors are
        correctly far apart.

        Examples:
            - Nearest transformer to a panel:
              reference_external_id=<panel id>, category='transformers'
            - Five closest anything:
              reference_external_id=<id>, limit=5

        Returns:
            {"reference_external_id": str, "count": int,
             "elements": [{external_id, asset_type, description, level_name,
             distance_ft}]} ordered nearest first.
        """
        if not valid_external_id(reference_external_id):
            return {
                "reference_external_id": reference_external_id,
                "count": 0,
                "elements": [],
                "hint": "reference_external_id is not a valid element id.",
            }
        if category is not None and category not in ALLOWED_CATEGORIES:
            return {
                "reference_external_id": reference_external_id,
                "count": 0,
                "elements": [],
                "hint": "Unsupported category.",
            }
        if level is not None and level not in ALLOWED_LEVELS:
            return {
                "reference_external_id": reference_external_id,
                "count": 0,
                "elements": [],
                "hint": "Unsupported level.",
            }
        bounded_limit = max(1, min(int(limit or 5), MAX_SPATIAL_LIMIT))
        payload: dict[str, Any] = {
            "op": "nearest",
            "reference_external_id": reference_external_id,
            "limit": bounded_limit,
        }
        if category is not None:
            payload["category"] = category
        if level is not None:
            payload["level"] = level
        try:
            data = await post_json(
                settings.viewer_base_url,
                "api/assets/spatial",
                payload,
            )
        except (UpstreamServiceError, ValueError):
            return {
                "reference_external_id": reference_external_id,
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
                    "distance_ft": row.get("distance_ft"),
                }
            )
        return {
            "reference_external_id": reference_external_id,
            "count": len(elements),
            "elements": elements,
        }
