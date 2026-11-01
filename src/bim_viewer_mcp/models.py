"""Pydantic models, enums, and validators for viewer-control tool parameters.

Typed parameters give MCP clients a precise schema, which helps the LLM call
the tools correctly. Every element and room is addressed by its durable Revit
UniqueId (the APS viewer's `externalId`) — never a transient numeric dbId.
"""

from __future__ import annotations

import re

# Revit UniqueId shape: an 8-4-4-4-12 hex GUID, a hyphen, then 8 hex digits.
# This is the only id shape the viewer accepts; validating here (in addition to
# the browser) treats every tool argument as untrusted.
EXTERNAL_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-[0-9a-f]{8}$",
    re.IGNORECASE,
)

# Cap so a hostile or oversized tool call can't flood the viewer. Mirrors the
# browser-side MAX_IDS.
MAX_IDS = 5000


def valid_external_id(value: object) -> bool:
    """True if value is a string matching the Revit UniqueId shape."""
    return isinstance(value, str) and EXTERNAL_ID_RE.match(value) is not None


def sanitize_external_ids(values: object) -> list[str]:
    """Keep only well-formed external ids, de-duplicated, capped at MAX_IDS."""
    if not isinstance(values, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if len(out) >= MAX_IDS:
            break
        if valid_external_id(v) and v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ── find_elements query vocabulary ────────────────────────────────────────────────────
# The find_elements tool resolves a COST criterion to externalIds by asking the
# viewer backend. The metric and operator are constrained to these fixed
# allowlists (defense in depth; the backend enforces the same). No free-form
# query, field name, or SQL is ever passed through.
ALLOWED_METRICS = {"replacement_cost", "maintenance_cost"}
ALLOWED_OPS = {"top", "gt", "gte", "lt", "lte"}
MAX_QUERY_LIMIT = 500


# ── find_by_category query vocabulary ─────────────────────────────────────────────────
# The find_by_category tool resolves a CATEGORY (plus optional level/room) to
# externalIds by asking the viewer backend. The category and level are
# constrained to these fixed allowlists (defense in depth; the backend enforces
# the same, and maps each category to its asset_type set server-side). No raw
# asset_type, free-form field, or SQL is ever passed through.
ALLOWED_CATEGORIES = {
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
}
ALLOWED_LEVELS = {"Level 1", "Level 2", "Level 3", "Roof Level"}
MAX_CATEGORY_LIMIT = 5000


# ── spatial query vocabulary (find_nearest / find_extreme) ────────────────────────────
# The spatial tools rank elements by TRUE 3D geometry (bounding-box gap,
# directional extreme, bbox volume) computed server-side in Python — Databricks
# ST_* functions are 2D-only and would read two elements on different floors as
# touching. The op is constrained to these fixed allowlists (defense in depth;
# the backend enforces the same). No coordinate or SQL is ever passed through.
ALLOWED_DIRECTIONS = {"northmost", "southmost", "eastmost", "westmost"}
ALLOWED_SIZE_OPS = {"largest", "smallest"}
MAX_SPATIAL_LIMIT = 50


# ── find_in_room query vocabulary ─────────────────────────────────────────────────────
# find_in_room resolves a room (by durable id or room number) to the tracked
# assets whose bounding-box center falls inside the room's 3D volume (its WKT
# footprint extruded from floor to ceiling). Membership math is done server-side
# in Python. An optional category restricts the result to one asset family.
MAX_INROOM_LIMIT = 500


# ── find_clearance query vocabulary ───────────────────────────────────────────────────
# find_clearance checks NEC 110.26 working clearance for one electrical panel.
# v1 covers electrical panels only; any other category returns "rule not
# defined". The backend owns the NEC numbers and all geometry.
CLEARANCE_CATEGORIES = {"electrical_panels"}


# ── find_workorders query vocabulary ──────────────────────────────────────────────────
# find_workorders counts or cost-aggregates maintenance WORK ORDERS under
# allowlisted filters by asking the viewer backend (POST api/workorders/find).
# Every filter is constrained to these fixed lists (defense in depth; the
# backend enforces the same); priority is a friendly word mapped to the stored
# number, and asset_category maps through ALLOWED_CATEGORIES to asset_type. No
# free-form field, value, or SQL is ever passed through. This tool returns
# numbers, not externalIds, and performs no viewer action.
WO_ALLOWED_STATUSES = {
    "OPEN", "IN_PROGRESS", "WAITING_PARTS", "CLOSED", "CANCELED",
}
# Lower number = more urgent (2 = high, 3 = normal, 4 = low), per the data
# generator. No priority 1 exists in the data (the emergency tier was removed).
WO_PRIORITY_WORDS = {"high", "normal", "low"}
WO_ALLOWED_FAILURE_CLASSES = {
    "LIGHTING", "ELECTRICAL", "HVAC", "PLUMBING", "FIRE_PROTECTION",
}
WO_ALLOWED_REPORTED_BY = {
    "TENANT", "FACILITIES_TECH", "INSPECTOR", "VENDOR",
}
WO_ALLOWED_MEASURES = {"count", "cost"}
