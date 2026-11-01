"""Tool: find_workorders — count or cost-total maintenance work orders.

Unlike the find_* element tools, this answers questions about WORK ORDERS /
maintenance tickets, not about building elements. It RETURNS numbers (a count
with a status breakdown, or spend/labor totals) by asking the viewer backend,
which queries the work-order table under fixed allowlisted filters.

Every filter is constrained to a server-side allowlist (or mapped from a
friendly word): level, status, priority (high/normal/low -> 2/3/4),
failure_class, asset_category (the equipment a work order services, e.g.
'heat_pumps' = WSHP), and reported_by_role. Filters combine with AND. No
free-form field, value, or SQL is ever passed through; the backend enforces the
same allowlists. The viewer backend origin is fixed by server config, never a
tool argument.

This tool performs NO viewer action — it returns data only.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.models import (
    ALLOWED_CATEGORIES,
    ALLOWED_LEVELS,
    WO_ALLOWED_FAILURE_CLASSES,
    WO_ALLOWED_MEASURES,
    WO_ALLOWED_REPORTED_BY,
    WO_ALLOWED_STATUSES,
    WO_PRIORITY_WORDS,
)
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the find_workorders tool with the MCP server."""

    @mcp.tool(
        name="find_workorders",
        annotations={
            "title": "Count or cost-total work orders",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def find_workorders(
        level: Annotated[
            Literal["Level 1", "Level 2", "Level 3", "Roof Level"] | None,
            "Optional building level to restrict to. Omit for all levels.",
        ] = None,
        status: Annotated[
            Literal[
                "OPEN",
                "IN_PROGRESS",
                "WAITING_PARTS",
                "CLOSED",
                "CANCELED",
            ]
            | None,
            "Optional work-order status (e.g. OPEN for open work orders).",
        ] = None,
        priority: Annotated[
            Literal["high", "normal", "low"] | None,
            "Optional priority as a word: high (most urgent), normal, or low.",
        ] = None,
        failure_class: Annotated[
            Literal[
                "LIGHTING",
                "ELECTRICAL",
                "HVAC",
                "PLUMBING",
                "FIRE_PROTECTION",
            ]
            | None,
            "Optional failure class (the system the work order is against).",
        ] = None,
        asset_category: Annotated[
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
            "Optional category of the equipment a work order services "
            "(e.g. 'heat_pumps' = the WSHP units).",
        ] = None,
        reported_by_role: Annotated[
            Literal["TENANT", "FACILITIES_TECH", "INSPECTOR", "VENDOR"] | None,
            "Optional role that reported the work order.",
        ] = None,
        measure: Annotated[
            Literal["count", "cost"],
            "'count' (default) returns how many plus a status breakdown; "
            "'cost' returns spend/labor totals (total_cost, "
            "total_labor_hours, avg_cost).",
        ] = "count",
    ) -> dict[str, Any]:
        """Count or cost-total maintenance work orders under fixed filters.

        Use this for work-order / maintenance questions, e.g. "how many work
        orders on Level 2", "open work orders", "high-priority work orders on
        the 1st floor that service the heat pumps", "how much have we spent on
        HVAC work orders". All filters are optional and combine with AND.

        Examples:
            - Work orders on Level 1:          level='Level 1'
            - Open work orders:                status='OPEN'
            - High-priority WSHP on Level 1:   level='Level 1',
                                               asset_category='heat_pumps',
                                               priority='high'
            - HVAC maintenance spend:          failure_class='HVAC',
                                               measure='cost'

        Returns (count):
            {"measure": "count", "filters": {...}, "count": int,
             "breakdown_by_status": {status: int}}.
        Returns (cost):
            {"measure": "cost", "filters": {...}, "count": int,
             "total_cost": float, "total_labor_hours": float,
             "avg_cost": float}.
        """
        measure_v = (measure or "count").lower()
        if measure_v not in WO_ALLOWED_MEASURES:
            return {"error": "unsupported measure"}
        if level is not None and level not in ALLOWED_LEVELS:
            return {"error": "unsupported level"}
        if status is not None and status.upper() not in WO_ALLOWED_STATUSES:
            return {"error": "unsupported status"}
        if priority is not None and str(priority).lower() not in WO_PRIORITY_WORDS:
            return {"error": "unsupported priority"}
        if (
            failure_class is not None
            and failure_class.upper() not in WO_ALLOWED_FAILURE_CLASSES
        ):
            return {"error": "unsupported failure_class"}
        if asset_category is not None and asset_category not in ALLOWED_CATEGORIES:
            return {"error": "unsupported asset_category"}
        if (
            reported_by_role is not None
            and reported_by_role.upper() not in WO_ALLOWED_REPORTED_BY
        ):
            return {"error": "unsupported reported_by_role"}

        payload: dict[str, Any] = {"measure": measure_v}
        if level is not None:
            payload["level"] = level
        if status is not None:
            payload["status"] = status
        if priority is not None:
            payload["priority"] = priority
        if failure_class is not None:
            payload["failure_class"] = failure_class
        if asset_category is not None:
            payload["asset_category"] = asset_category
        if reported_by_role is not None:
            payload["reported_by_role"] = reported_by_role

        try:
            data = await post_json(
                settings.viewer_base_url,
                "api/workorders/find",
                payload,
            )
        except (UpstreamServiceError, ValueError):
            return {"error": "The viewer backend could not be reached."}

        return data if isinstance(data, dict) else {"error": "bad response"}
