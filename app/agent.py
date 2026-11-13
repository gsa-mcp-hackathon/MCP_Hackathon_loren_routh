"""Fast agent path: a USAi LLM calls the asset tools, then the viewer highlights.

This is the second, FAST chat path, alongside the existing Genie path in app.py.
The browser chat box can POST here (`/api/agent`) instead of `/api/chat`:

    browser chat box
      -> POST /api/agent
         -> USAi LLM (the same model the Phoenix eval scored)
            -> decides & calls the asset tools below (find_by_category /
               find_elements / highlight_elements)
               -> tools run the same allowlisted SQL the /api/assets/* routes
                  use, and broadcast highlight over the viewer command hub
                  -> every connected viewer highlights the result

Genie (app.py `/api/chat`) is untouched and remains the conversational
fallback. This module only ADDS a route; it imports the allowlists, the query
helper, and the viewer hub from app.py so there is a single source of truth and
no duplicated SQL vocabulary.

Credentials come from the user's .env via GSA_AI_ENV_PATH (USAI_API_KEY /
USAI_BASE_URL / USAI_MODEL or AGENT_MODEL), exactly like the eval harness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from fastapi import APIRouter, HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel

from db import query_rows

log = logging.getLogger("viewer")

# These are imported from app.py at call time (not import time) to avoid a
# circular import: app.py imports this module. See _app() below.


# ----------------------------------------------------------------- credentials

def _load_usai_env() -> None:
    """Load USAi credentials from the user's .env via GSA_AI_ENV_PATH.

    No new dependency: a tiny KEY=VALUE reader. Only sets variables that are
    not already present in the environment, so an explicit env override wins.
    """
    path = os.environ.get("GSA_AI_ENV_PATH")
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        log.warning("agent_env_load_skipped")


def _llm() -> tuple[AsyncOpenAI, str]:
    _load_usai_env()
    key = os.environ.get("USAI_API_KEY")
    base = (os.environ.get("USAI_BASE_URL") or "").rstrip("/")
    model = os.environ.get("AGENT_MODEL") or os.environ.get("USAI_MODEL")
    if not (key and base and model):
        raise HTTPException(
            status_code=503,
            detail="Fast agent not configured (USAi credentials missing).",
        )
    # USAI_BASE_URL is the gateway root; the OpenAI-compatible path is /api/v1.
    client = AsyncOpenAI(api_key=key, base_url=base + "/api/v1")
    return client, model


# -------------------------------------------------------------- app references

def _app():
    """Return the live app module (lazy import avoids a circular import)."""
    import app  # app.py imports this module, so import here, not at top level.

    return app


# ------------------------------------------------------------------- the tools
#
# Each tool reuses the SAME allowlists and query shape as the existing
# /api/assets/* routes (imported from app.py), so there is one source of truth
# and no duplicated SQL vocabulary. The tools return small summaries plus the
# external_ids; the route collects the ids it should highlight.

def _find_by_category(category: str, level: str | None, room_name: str | None,
                      limit: int) -> dict:
    app = _app()
    if category not in app.ALLOWED_CATEGORIES:
        return {"count": 0, "elements": [],
                "valid_categories": sorted(app.ALLOWED_CATEGORIES)}
    if level is not None and level not in app.ALLOWED_LEVELS:
        return {"count": 0, "elements": [],
                "valid_levels": sorted(app.ALLOWED_LEVELS)}
    bounded = max(1, min(int(limit or 500), app.MAX_CATEGORY_LIMIT))
    asset_types = app.ALLOWED_CATEGORIES[category]
    type_list = ", ".join(f"'{t}'" for t in asset_types)
    where_extra = ""
    params: dict = {}
    if level is not None:
        where_extra += " AND level_name = :level"
        params["level"] = level
    if room_name:
        where_extra += " AND lower(room_name) LIKE :room"
        params["room"] = f"%{room_name.strip().lower()}%"
    for i, term in enumerate(app.CATEGORY_DESCRIPTION_EXCLUDES.get(category, [])):
        where_extra += f" AND lower(description) NOT LIKE :excl{i}"
        params[f"excl{i}"] = f"%{term}%"
    query = (
        "SELECT source_unique_id AS external_id, asset_type, description, "
        "       level_name, room_name, replacement_cost, "
        "       manufacturer, model_number, serial_number "
        f"FROM {app.ASSETS_TABLE} "
        "WHERE source_unique_id IS NOT NULL "
        f"  AND asset_type IN ({type_list})"
        f"{where_extra} "
        "ORDER BY level_name, asset_type "
        f"LIMIT {bounded}"
    )
    rows = query_rows(query, params)
    return {"category": category, "count": len(rows), "elements": rows}


def _find_elements(metric: str, op: str, value: float, limit: int) -> dict:
    app = _app()
    if metric not in app.ALLOWED_METRICS or op not in app.ALLOWED_OPS:
        return {"count": 0, "elements": [],
                "valid_metrics": sorted(app.ALLOWED_METRICS),
                "valid_ops": sorted(app.ALLOWED_OPS)}
    bounded = max(1, min(int(limit or 50), app.MAX_LIMIT))
    metric_sql = app.ALLOWED_METRICS[metric]
    base = (
        f"FROM {app.ASSETS_TABLE} a "
        f"LEFT JOIN (SELECT asset_id, SUM(total_cost) AS total_maintenance_cost "
        f"           FROM {app.WORKORDERS_TABLE} GROUP BY asset_id) w "
        f"  ON a.asset_id = w.asset_id "
        f"WHERE a.source_unique_id IS NOT NULL"
    )
    params: dict = {}
    if op in ("top", "bottom"):
        where_extra = ""
    else:
        sql_op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
        where_extra = f" AND {metric_sql} {sql_op} :value"
        params["value"] = float(value or 0.0)
    direction = "ASC" if op == "bottom" else "DESC"
    query = (
        f"SELECT a.source_unique_id AS external_id, a.asset_type, "
        f"       a.description, a.level_name, a.room_name, "
        f"       a.manufacturer, a.model_number, a.serial_number, "
        f"       {metric_sql} AS metric_value "
        f"{base}{where_extra} ORDER BY {metric_sql} {direction} LIMIT {bounded}"
    )
    rows = query_rows(query, params)
    return {"metric": metric, "count": len(rows), "elements": rows}


def _find_workorders(level: str | None, status: str | None,
                     priority: str | None, failure_class: str | None,
                     asset_category: str | None, reported_by_role: str | None,
                     measure: str | None) -> dict:
    """Count or cost-aggregate work orders under allow-listed filters only.

    Every filter is validated against a fixed list (or mapped from a friendly
    word) before it reaches SQL; the model never writes a query. This is a data
    question, not a viewer action: it returns numbers, highlights nothing.
    """
    app = _app()
    measure = (measure or "count").lower()
    if measure not in app.WO_ALLOWED_MEASURES:
        return {"error": "unsupported measure",
                "valid_measures": sorted(app.WO_ALLOWED_MEASURES)}

    filters: dict = {}  # human-readable echo of what was applied
    where: list[str] = []
    params: dict = {}

    if level is not None:
        if level not in app.ALLOWED_LEVELS:
            return {"error": "unsupported level",
                    "valid_levels": sorted(app.ALLOWED_LEVELS)}
        where.append("level_name = :level")
        params["level"] = level
        filters["level"] = level
    if status is not None:
        s = status.upper()
        if s not in app.WO_ALLOWED_STATUSES:
            return {"error": "unsupported status",
                    "valid_statuses": sorted(app.WO_ALLOWED_STATUSES)}
        where.append("status = :status")
        params["status"] = s
        filters["status"] = s
    if priority is not None:
        p = app.WO_PRIORITY_WORDS.get(str(priority).lower())
        if p is None:
            return {"error": "unsupported priority",
                    "valid_priorities": sorted(app.WO_PRIORITY_WORDS)}
        where.append("priority = :priority")
        params["priority"] = p
        filters["priority"] = str(priority).lower()
    if failure_class is not None:
        f = failure_class.upper()
        if f not in app.WO_ALLOWED_FAILURE_CLASSES:
            return {"error": "unsupported failure_class",
                    "valid_failure_classes": sorted(
                        app.WO_ALLOWED_FAILURE_CLASSES)}
        where.append("failure_class = :failure_class")
        params["failure_class"] = f
        filters["failure_class"] = f
    if asset_category is not None:
        if asset_category not in app.ALLOWED_CATEGORIES:
            return {"error": "unsupported asset_category",
                    "valid_categories": sorted(app.ALLOWED_CATEGORIES)}
        asset_types = app.ALLOWED_CATEGORIES[asset_category]
        type_list = ", ".join(f"'{t}'" for t in asset_types)
        where.append(f"asset_type IN ({type_list})")
        filters["asset_category"] = asset_category
    if reported_by_role is not None:
        r = reported_by_role.upper()
        if r not in app.WO_ALLOWED_REPORTED_BY:
            return {"error": "unsupported reported_by_role",
                    "valid_roles": sorted(app.WO_ALLOWED_REPORTED_BY)}
        where.append("reported_by_role = :reported_by_role")
        params["reported_by_role"] = r
        filters["reported_by_role"] = r

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    if measure == "cost":
        query = (
            "SELECT COUNT(*) AS n, "
            "ROUND(SUM(total_cost), 2) AS total_cost, "
            "ROUND(SUM(labor_hours), 1) AS total_labor_hours, "
            "ROUND(AVG(total_cost), 2) AS avg_cost "
            f"FROM {app.WORKORDERS_TABLE}{where_sql}"
        )
        row = (query_rows(query, params) or [{}])[0]
        return {
            "measure": "cost",
            "filters": filters,
            "count": int(row.get("n") or 0),
            "total_cost": row.get("total_cost"),
            "total_labor_hours": row.get("total_labor_hours"),
            "avg_cost": row.get("avg_cost"),
        }

    # measure == count: total plus a small status breakdown.
    total_q = f"SELECT COUNT(*) AS n FROM {app.WORKORDERS_TABLE}{where_sql}"
    total = int((query_rows(total_q, params) or [{}])[0].get("n") or 0)
    bd_q = (
        "SELECT status, COUNT(*) AS n "
        f"FROM {app.WORKORDERS_TABLE}{where_sql} "
        "GROUP BY status ORDER BY n DESC"
    )
    breakdown = {r["status"]: int(r["n"]) for r in query_rows(bd_q, params)}
    return {
        "measure": "count",
        "filters": filters,
        "count": total,
        "breakdown_by_status": breakdown,
    }


async def _find_in_room(room_number: str | None, room_external_id: str | None,
                        category: str | None, limit: int) -> dict:
    """Assets inside a room's 3D volume, via the existing /api/assets/in-room."""
    app = _app()
    body = app.InRoomQueryIn(
        room_number=room_number,
        room_external_id=room_external_id,
        category=category,
        limit=int(limit or 50),
    )
    return await app.assets_in_room(body)


async def _find_spatial(op: str, category: str | None, level: str | None,
                        reference_external_id: str | None, limit: int) -> dict:
    """Rank elements by true 3D geometry, via the existing /api/assets/spatial.

    Serves both find_extreme (largest/smallest/compass) and find_nearest
    (op='nearest', needs a reference element).
    """
    app = _app()
    body = app.SpatialQueryIn(
        op=op,
        category=category,
        level=level,
        reference_external_id=reference_external_id,
        limit=int(limit or 1),
    )
    return await app.assets_spatial(body)


async def _find_clearance(external_id: str | None,
                          description: str | None = None) -> dict:
    """NEC 110.26 working-clearance check, via /api/assets/clearance.

    Targets one panel by its externalId, or by a description substring that the
    backend resolves to exactly one electrical panel (so 'the circuit breaker
    switchboard' works without carrying an id).
    """
    app = _app()
    body = app.ClearanceQueryIn(
        category="electrical_panels",
        external_id=external_id,
        description=description,
    )
    return await app.assets_clearance(body)


# The tool schemas advertised to the model. Categories/levels mirror app.py.
def _tool_specs() -> list[dict]:
    app = _app()
    categories = sorted(app.ALLOWED_CATEGORIES)
    levels = sorted(app.ALLOWED_LEVELS)
    return [
        {
            "type": "function",
            "function": {
                "name": "find_by_category",
                "description": (
                    "Find building elements by category (optionally restricted "
                    "to a level and/or a room-name substring) AND highlight them "
                    "in the 3D viewer. Use for questions like 'the transformers', "
                    "'air terminals on Level 2', 'the cooling tower'. "
                    "'air_terminals' covers supply diffusers, return grilles, "
                    "and exhaust grilles; 'heat_pumps' is the water-source heat "
                    "pumps (WSHP). You do not need to call highlight_elements "
                    "after this."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": categories},
                        "level": {"type": "string", "enum": levels},
                        "room_name": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["category"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_elements",
                "description": (
                    "Find building elements by a COST criterion AND highlight "
                    "them in the 3D viewer. metric is replacement_cost or "
                    "maintenance_cost; op is 'top' (highest N, use for 'most "
                    "expensive'), 'bottom' (lowest N, use for 'least/cheapest "
                    "expensive'), or gt/gte/lt/lte against a dollar threshold. "
                    "For 'most/least expensive' use op top/bottom with limit 1 "
                    "and no value. Use for 'most expensive to replace', "
                    "'equipment over $50,000', etc. You do not need to call "
                    "highlight_elements after this."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "metric": {
                            "type": "string",
                            "enum": sorted(app.ALLOWED_METRICS),
                        },
                        "op": {
                            "type": "string",
                            "enum": sorted(app.ALLOWED_OPS),
                        },
                        "value": {"type": "number"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["metric", "op"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_workorders",
                "description": (
                    "Count or cost-total MAINTENANCE WORK ORDERS, optionally "
                    "filtered. This answers questions about work orders / "
                    "maintenance tickets \u2014 NOT about building elements. Use "
                    "it for 'how many work orders on Level 2', 'open work "
                    "orders', 'high-priority work orders on the 1st floor that "
                    "service the heat pumps', 'how much have we spent on HVAC "
                    "work orders'. All filters are optional and combine with "
                    "AND. priority is the word high/normal/low. asset_category "
                    "restricts to the equipment a work order services (e.g. "
                    "'heat_pumps' = WSHP). Set measure='cost' for spend/labor "
                    "totals, otherwise it returns a count with a status "
                    "breakdown. This tool returns numbers only; it does NOT "
                    "highlight anything in the viewer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "level": {"type": "string", "enum": levels},
                        "status": {
                            "type": "string",
                            "enum": sorted(app.WO_ALLOWED_STATUSES),
                        },
                        "priority": {
                            "type": "string",
                            "enum": sorted(app.WO_PRIORITY_WORDS),
                        },
                        "failure_class": {
                            "type": "string",
                            "enum": sorted(app.WO_ALLOWED_FAILURE_CLASSES),
                        },
                        "asset_category": {
                            "type": "string",
                            "enum": categories,
                        },
                        "reported_by_role": {
                            "type": "string",
                            "enum": sorted(app.WO_ALLOWED_REPORTED_BY),
                        },
                        "measure": {
                            "type": "string",
                            "enum": sorted(app.WO_ALLOWED_MEASURES),
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_in_room",
                "description": (
                    "Find the tracked assets that sit inside a ROOM's 3D "
                    "volume, identified by its ROOM NUMBER (e.g. 'room 54') or "
                    "the room's externalId. This matches a room NUMBER exactly "
                    "\u2014 it does NOT look a room up by NAME. For a room "
                    "referred to by name (e.g. 'the Physics room', 'the "
                    "electrical room'), use find_by_category with its room_name "
                    "filter instead. Optionally restrict to one category. "
                    "Returns element data (external_ids); it does NOT change "
                    "the view \u2014 to show the result, call highlight_elements "
                    "or isolate with the returned ids."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "room_number": {"type": "string"},
                        "room_external_id": {"type": "string"},
                        "category": {"type": "string", "enum": categories},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_extreme",
                "description": (
                    "Find the single most-EXTREME element by geometry: op is "
                    "'largest'/'smallest' (by 3D bounding-box volume) or a "
                    "compass direction 'northmost'/'southmost'/'eastmost'/"
                    "'westmost' (by position). Use for 'the largest piece of "
                    "equipment', 'the northmost air handler', 'the smallest "
                    "transformer'. Optionally restrict by category and/or level. "
                    "Returns element data (external_ids); it does NOT change the "
                    "view \u2014 to show it, call highlight_elements, isolate, "
                    "or zoom_to with the returned ids."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": sorted(app.ALLOWED_SPATIAL_OPS - {"nearest"}),
                        },
                        "category": {"type": "string", "enum": categories},
                        "level": {"type": "string", "enum": levels},
                        "limit": {"type": "integer"},
                    },
                    "required": ["op"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_nearest",
                "description": (
                    "Find the element(s) CLOSEST to a given reference element, "
                    "ranked by true 3D distance. reference_external_id is the "
                    "externalId to measure FROM (get it from find_by_category "
                    "first). Use for 'the nearest transformer to this panel', "
                    "'what is closest to the cooling tower'. Optionally restrict "
                    "the candidates by category and/or level. Returns element "
                    "data (external_ids); it does NOT change the view \u2014 to "
                    "show it, call highlight_elements, isolate, or zoom_to."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reference_external_id": {"type": "string"},
                        "category": {"type": "string", "enum": categories},
                        "level": {"type": "string", "enum": levels},
                        "limit": {"type": "integer"},
                    },
                    "required": ["reference_external_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_clearance",
                "description": (
                    "Check NEC 110.26 working CLEARANCE for one electrical "
                    "panel: the code-required working space in front of the "
                    "panel, and whether anything intrudes into it, AND draw the "
                    "clearance zones in the viewer. Target the panel EITHER by "
                    "external_id (its externalId, e.g. from a previous result) "
                    "OR by description \u2014 a substring of the panel's name, "
                    "e.g. 'circuit breaker switchboard' \u2014 which the backend "
                    "resolves to the single matching panel. Prefer description "
                    "when the user names the panel (e.g. 'the circuit breaker "
                    "switchboard'); you do NOT need to run find_by_category "
                    "first. Use for 'is the working space in front of this panel "
                    "clear?'. If the description matches more than one panel the "
                    "tool reports that \u2014 ask the user which one."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "external_id": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "highlight_rooms",
                "description": (
                    "Highlight a set of ROOMS (spaces) in the 3D viewer, in "
                    "place. Pass the rooms' externalIds. Use this to show rooms, "
                    "as opposed to highlight_elements which shows equipment."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "space_external_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                    },
                    "required": ["space_external_ids"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "isolate",
                "description": (
                    "ISOLATE a set of elements: show only them and hide "
                    "everything else in the 3D viewer. Pass the external_ids "
                    "(e.g. from a find_* tool). Use when the user wants to see "
                    "ONLY the matching elements with the rest of the model "
                    "hidden, rather than highlighted in context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "external_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                    },
                    "required": ["external_ids"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "zoom_to",
                "description": (
                    "Move the CAMERA to frame a set of elements or a single "
                    "room. This is the only tool that moves the camera. Pass "
                    "external_ids to frame elements, OR space_external_id to "
                    "frame one room. Use for 'zoom in on ...', 'focus on ...'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "external_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "space_external_id": {"type": "string"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reset",
                "description": (
                    "RESET the 3D viewer: clear all highlighting/isolation and "
                    "return to the default view. Use for 'reset', 'clear the "
                    "selection', 'show everything again'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "highlight_elements",
                "description": (
                    "Highlight a set of building elements in the 3D viewer, in "
                    "place (the camera does not move). Pass the external_ids you "
                    "got from find_by_category or find_elements. Call this to "
                    "show the answer visually."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "external_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                    },
                    "required": ["external_ids"],
                },
            },
        },
    ]


SYSTEM = (
    "You control a live 3D viewer of a mechanical building model. To answer a "
    "question, call find_by_category or find_elements with the right arguments. "
    "Those tools ALSO highlight what they find in the viewer automatically, so "
    "you normally do NOT need to call highlight_elements afterward and you do "
    "NOT need to repeat the element ids back. Call highlight_elements only to "
    "refine a selection to a specific subset of ids the user asked for. Never "
    "invent ids. A pricing question like 'how much does a light/transformer "
    "cost' IS in scope: answer it by calling find_by_category for that category "
    "\u2014 the viewer reply reports the price range automatically, so always "
    "call the tool rather than refusing. "
    "For questions about WORK ORDERS or MAINTENANCE (how many work orders, open "
    "work orders, high-priority work orders, how much maintenance has cost, "
    "etc.) call find_workorders. It returns numbers, not a highlight: state the "
    "answer in one short sentence from the values it returns \u2014 the count "
    "(and, for a cost question, the total_cost / total_labor_hours). Translate "
    "the priority back to the word high/normal/low. Do not call a highlight "
    "tool for a work-order question. "
    "You also have tools that find elements by location or geometry: "
    "find_in_room (what assets are inside a room, by its room NUMBER), "
    "find_extreme "
    "(the largest/smallest or most northmost/southmost/eastmost/westmost "
    "element), and find_nearest (the elements closest to a reference element). "
    "These RETURN element data and do NOT change the view on their own \u2014 so "
    "if the user wants to SEE the result, follow up by calling highlight_elements "
    "(show in place), isolate (show only those, hide the rest), or zoom_to (move "
    "the camera) with the external_ids the find tool returned. IMPORTANT: match "
    "the act tool to the user's verb \u2014 if they say 'zoom in' / 'zoom to' / "
    "'focus on', you MUST follow up with zoom_to (not highlight_elements); use "
    "highlight_elements only when they say 'show' / 'highlight'. A room referred "
    "to by NAME (e.g. 'the Physics room', 'the electrical room') is NOT a room "
    "number \u2014 resolve it with find_by_category's room_name filter, never "
    "find_in_room. Use find_in_room ONLY when the user gives an actual room "
    "NUMBER (e.g. 'room 54'). For example 'show "
    "the largest air handler' = find_extreme then highlight_elements; 'zoom in on "
    "the equipment in room 54' = find_in_room then zoom_to; 'zoom in on "
    "the air terminals in the Physics room on Level 3' = find_by_category "
    "(category=air_terminals, level='Level 3', room_name='Physics') then zoom_to. "
    "find_clearance checks "
    "ONE electrical panel's NEC working clearance and draws the zones. Target it "
    "by description when the user names the panel \u2014 e.g. 'the circuit "
    "breaker switchboard' = find_clearance with description='circuit breaker "
    "switchboard' (do NOT run find_by_category first). If the user asks about "
    "the panels in GENERAL (plural, e.g. 'are the electrical panels clear?'), do "
    "NOT try to check all of them: check the circuit breaker switchboard as a "
    "representative example by calling find_clearance with "
    "description='circuit breaker switchboard', state its result, and briefly "
    "offer to check a specific other panel if they name one. Do NOT ask an "
    "open-ended numbered menu. If find_clearance reports no "
    "match or more than one match, ask the user to clarify which panel. "
    "highlight_rooms shows rooms; reset clears everything "
    "and returns to the default view. Never invent external_ids \u2014 only pass "
    "ids a find tool returned. "
    "Use an enum value (a level, status, priority, failure class, category, or "
    "reported-by role) only when the user's own words clearly ARE that value or "
    "an obvious synonym of it \u2014 for example 'the 1st floor' is 'Level 1', "
    "'heat pumps' is 'heat_pumps', 'open tickets' is status OPEN. Do NOT "
    "substitute a different concept for the nearest allowed value: if the user "
    "names something the allowed values do not cover (for example a trade like "
    "'plumber' or 'electrician', which is NOT the same as the reported-by roles "
    "tenant/facilities_tech/inspector/vendor), do not quietly map it to a "
    "similar value \u2014 say the data does not track that and, if useful, name "
    "what it does track. When in doubt, decline rather than guess. "
    "When a find tool returns a small result, its summary includes each "
    "element's attributes (type, description, level, room, replacement_cost, "
    "manufacturer, model_number, serial_number). If the user asked about a "
    "specific attribute \u2014 for example the model, serial number, or "
    "manufacturer \u2014 answer it in one short sentence using those values, and "
    "only state values that are present (if a field is missing, say it isn't "
    "available). For a plain 'find/show/highlight the X' request with no "
    "specific attribute asked, do not add a message \u2014 the viewer already "
    "shows the result. If a request is genuinely outside these tools, say so "
    "briefly."
)

MAX_TURNS = 6

# A result at or under this size is small enough that we let the model take one
# extra turn to narrate from the elements' attributes (so "what model/serial is
# the X?" gets a real answer), and we include those attributes in the tool
# summary. Above this, we stay id-light and skip narration for speed.
SMALL_RESULT = 5

# Phrases that mean the user wants an ACTION on the result (a follow-up act
# tool), not just "show me X". When the request reads like one of these, we do
# NOT early-stop after a find tool's auto-highlight, so the model gets the extra
# turn it needs to chain into zoom_to / isolate / find_clearance. Plain "show
# me / where is / find the X" keeps the fast single-turn early-stop.
_ACTION_HINTS = (
    "zoom", "isolate", "clearance", "working space", "working clearance",
    "in front of", "close up", "focus on", "frame ",
)


def _implies_action(question: str) -> bool:
    q = (question or "").lower()
    return any(h in q for h in _ACTION_HINTS)


# Phrases that specifically mean "hide everything else and show only this" \u2014
# an isolate, not a plain zoom. Anything else in _ACTION_HINTS (zoom / focus /
# close up / frame) is treated as a zoom.
_ISOLATE_HINTS = ("isolate", "only it", "just the", "only the", "by itself")


def _action_command(question: str) -> str | None:
    """Classify an action request into the viewer command it should drive.

    Returns 'isolate' when the phrasing asks to see only the target, 'zoom_to'
    for any other zoom/focus phrasing, or None when the request implies no
    action. Used to deterministically chain a find into the act command with
    the ids the find just resolved \u2014 so the act never depends on the model
    chaining a second tool call or on stale server memory.
    """
    q = (question or "").lower()
    if any(h in q for h in _ISOLATE_HINTS):
        return "isolate"
    if any(h in q for h in _ACTION_HINTS):
        return "zoom_to"
    return None


# Act commands that drive the viewer directly over the command channel
# (isolate / zoom_to / highlight_rooms). When one ran this turn, the viewer is
# already in the state the user asked for. The browser's applyResponse() must
# then NOT reset the view (clearHighlight/showAll) over it \u2014 otherwise it
# un-does the act (e.g. un-isolates the set, flickering back to the whole
# building). We signal this with a `drove_viewer` flag on the response, which
# applyResponse() checks the same way it already checks `drew_clearance`.
_DROVE_VIEWER_COMMANDS = {"isolate", "zoom_to", "highlight_rooms"}


def _drove_viewer(resolved: list[dict]) -> bool:
    return any(
        r.get("kind") == "act" and r.get("command") in _DROVE_VIEWER_COMMANDS
        for r in resolved
    )


# Server-side conversational memory, simplest form: one global "last result"
# updated on every /api/agent turn and read by the next turn to resolve
# follow-ups like "highlight that equipment" or "show the clearance for it".
# Genie path never touches this. Single-viewer demo scope by design.
_LAST = {
    "external_ids": [],      # ids resolved by the last find tool
    "labels": [],            # short human labels, same order
    "category": None,        # last category queried (if any)
    "panel_external_id": None,  # last single electrical-panel id (for clearance)
}


def _remember(highlighted: dict, resolved: list) -> None:
    """Capture the last turn's result so the next turn can refer back to it."""
    ids = list(highlighted.keys())
    if ids:
        _LAST["external_ids"] = ids
        _LAST["labels"] = [
            (highlighted[i].get("label") or "") for i in ids
        ]
    for r in resolved:
        if r.get("kind") == "category" and r.get("category"):
            _LAST["category"] = r.get("category")
    # A single electrical panel is the natural subject of a clearance follow-up.
    if resolved and resolved[-1].get("category") == "electrical_panels" \
            and len(ids) == 1:
        _LAST["panel_external_id"] = ids[0]


def _memory_context() -> str | None:
    """A short system note describing the previous result, if any.

    Lets the model resolve 'that equipment / it / the panel' against the last
    turn WITHOUT re-running a find tool, and WITHOUT ever inventing ids: it
    reads the ids from this note. Omitted when there is nothing to recall.
    """
    ids = _LAST.get("external_ids") or []
    if not ids:
        return None
    labels = _LAST.get("labels") or []
    # Keep it compact: up to a handful of labelled ids, then a count.
    shown = []
    for i, ext in enumerate(ids[:SMALL_RESULT]):
        lbl = labels[i] if i < len(labels) else ""
        shown.append(f"{ext}" + (f" ({lbl})" if lbl else ""))
    more = len(ids) - len(shown)
    lines = [
        "PREVIOUS RESULT (for follow-ups like 'that equipment', 'it', "
        "'the panel'): the last find returned these externalIds:",
        "; ".join(shown) + (f"; and {more} more" if more > 0 else ""),
    ]
    if _LAST.get("category"):
        lines.append(f"Their category was '{_LAST['category']}'.")
    if _LAST.get("panel_external_id"):
        lines.append(
            "For a clearance follow-up, the panel externalId is "
            f"{_LAST['panel_external_id']}."
        )
    if _LAST.get("category") == "electrical_panels":
        lines.append(
            "To check the working clearance of a specific panel the user names "
            "(e.g. 'the circuit breaker switchboard'), call find_clearance with "
            "description set to that name \u2014 do not guess an id from the "
            "list above."
        )
    lines.append(
        "If the user refers back to this result, reuse these exact "
        "externalIds in highlight_elements / isolate / zoom_to / "
        "find_clearance \u2014 do NOT re-run a find tool and do NOT invent ids."
    )
    return "\n".join(lines)


router = APIRouter()


class AgentIn(BaseModel):
    query: str


@router.post("/api/agent")
async def agent(body: AgentIn):
    """Fast path: USAi LLM calls the asset tools, then the viewer highlights.

    Returns {reply, elements} in the same shape the viewer's chat box already
    consumes, so highlighting works with no viewer-render change. The viewer is
    also driven directly via the command hub for any other connected clients.
    """
    question = (body.query or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="query is required")

    client, model = _llm()
    tools = _tool_specs()
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM},
    ]
    # Conversational memory: if a previous turn resolved a result, give the
    # model a short note about it so follow-ups ("that equipment", "it",
    # "the panel") resolve against real ids instead of failing. Genie untouched.
    mem = _memory_context()
    if mem:
        messages.append({"role": "system", "content": mem})
    messages.append({"role": "user", "content": question})

    # Does the user's phrasing imply a follow-up action (zoom/isolate/
    # clearance)? If so, suppress the find tools' large-result early-stop so the
    # model keeps its turn to chain into the act tool.
    wants_action = _implies_action(question)
    # If the phrasing implies a camera action, which command it maps to
    # (isolate vs zoom_to). We use this to deterministically drive the viewer
    # with the ids the find resolved THIS turn \u2014 so the act never targets a
    # previous turn's elements and never depends on the model remembering to
    # chain a second tool call.
    action_command = _action_command(question)

    # Collected across tool calls so the viewer's result list / CSV match.
    highlighted: dict[str, dict] = {}
    # Track what the find tools resolved so we can narrate without an LLM turn.
    resolved: list[dict] = []
    # If a tool's query fails (e.g. expired token), carry a plain-language
    # message so the reply is an honest error, not a fake "Highlighted..." line.
    tool_error: list[str] = []
    did_highlight = False

    t_start = time.perf_counter()
    llm_calls = 0
    llm_seconds = 0.0
    tool_seconds = 0.0

    for _ in range(MAX_TURNS):
        t_llm = time.perf_counter()
        try:
            completion = await client.chat.completions.create(
                model=model, messages=messages, tools=tools, temperature=0,
            )
        except Exception as exc:
            log.warning("agent_model_call_failed")
            raise HTTPException(
                status_code=502, detail="Model call failed"
            ) from exc
        dt_llm = time.perf_counter() - t_llm
        llm_calls += 1
        llm_seconds += dt_llm
        log.debug("agent_llm_call n=%d took=%.2fs", llm_calls, dt_llm)

        reply = completion.choices[0].message
        if not reply.tool_calls:
            # Don't re-broadcast a highlight over a clearance overlay drawn this
            # turn (it would isolate() the ids and hide the boxes' context), nor
            # over an act command (isolate / zoom_to / highlight_rooms) that
            # already drove the viewer \u2014 see _drove_viewer.
            drew_clearance = any(
                r.get("kind") == "clearance" and r.get("drew")
                for r in resolved
            )
            drove_viewer = _drove_viewer(resolved)
            if not drew_clearance and not drove_viewer:
                await _broadcast(list(highlighted.keys()))
            _remember(highlighted, resolved)
            log.debug(
                "agent_done total=%.2fs llm_calls=%d llm=%.2fs tools=%.2fs",
                time.perf_counter() - t_start, llm_calls, llm_seconds,
                tool_seconds,
            )
            return {
                "reply": reply.content or "(no answer)",
                "elements": list(highlighted.values()),
                "drew_clearance": drew_clearance,
                "drove_viewer": drove_viewer,
            }

        messages.append(reply.model_dump(exclude_none=True))
        for call in reply.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except ValueError:
                args = {}
            t_tool = time.perf_counter()
            result = await _run_tool(name, args, highlighted, resolved, tool_error)
            dt_tool = time.perf_counter() - t_tool
            tool_seconds += dt_tool
            log.debug("agent_tool name=%s took=%.2fs", name, dt_tool)
            if name == "highlight_elements":
                did_highlight = True
            # The act tools (highlight_rooms / isolate / zoom_to / reset) drive
            # the viewer directly and need no narration \u2014 end the turn like
            # highlight_elements so we compose the one-line reply ourselves.
            if name in ("highlight_rooms", "isolate", "zoom_to", "reset"):
                did_highlight = True
            # Lever #2: the find tools "find AND show". They broadcast their own
            # result to the viewer right here (the backend already has the ids),
            # and for a LARGE result we end the turn immediately after \u2014 so
            # the model never gets a second turn in which it would transcribe a
            # large id list. This keeps a 104-element selection as fast as a
            # 4-element one. For a SMALL result we still highlight now, but let
            # the model take one more turn to narrate from the attributes the
            # summary carries (so attribute questions get a real answer).
            if name in ("find_by_category", "find_elements"):
                await _broadcast(list(highlighted.keys()))
                total = len(highlighted)
                # Normally a large (or empty) result ends the turn immediately
                # for speed. But when the user's phrasing implies a follow-up
                # action (zoom/isolate/clearance), keep the turn so the model
                # can chain into the act tool with these same ids.
                if (total == 0 or total > SMALL_RESULT) and not wants_action:
                    did_highlight = True
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result),
            })

        # A tool's query failed (e.g. the Databricks token expired). Surface the
        # real error as the reply instead of the cheery "Highlighted..." line,
        # and do not claim anything was highlighted.
        if tool_error:
            log.info(
                "agent_tool_error total=%.2fs llm_calls=%d: %s",
                time.perf_counter() - t_start, llm_calls, tool_error[0],
            )
            return {"reply": tool_error[0], "elements": []}

        # Deterministic find->act chaining. When the user's phrasing implies a
        # camera action (zoom / isolate / focus) and a find tool resolved ids
        # THIS turn, drive the viewer ourselves with exactly those fresh ids \u2014
        # instead of hoping the model chains a second act tool call (flaky at
        # temperature 0) or reuses stale ids from server memory (which made a
        # zoom target the PREVIOUS turn's elements). We only do this when the
        # model did not already drive the viewer this turn, so an explicit
        # model act call is never double-sent.
        if (
            action_command
            and highlighted
            and not _drove_viewer(resolved)
        ):
            act_ids = list(highlighted.keys())
            await _broadcast_command(action_command, {"external_ids": act_ids})
            resolved.append({"kind": "act", "command": action_command,
                             "count": len(act_ids)})
            did_highlight = True

        # Lever #1: once the viewer has been told to highlight, we have the
        # answer. Stop here and compose the one-line reply ourselves instead of
        # paying for another LLM round-trip just to narrate. (Combined with the
        # system prompt's "find + highlight in the same turn", a typical request
        # finishes in a SINGLE LLM call.)
        if did_highlight:
            # If a clearance was drawn this turn, the overlay (boxes + tinted
            # intruders + panel) is already on screen and depends on the whole
            # building staying visible. A trailing highlight_elements broadcast
            # would isolate() just the collected ids and hide that context,
            # erasing the boxes \u2014 so skip the re-broadcast in that case.
            # Likewise for an act command (isolate / zoom_to / highlight_rooms):
            # it already drove the viewer, and both the trailing re-broadcast
            # here AND the browser's applyResponse() reset would undo it (e.g.
            # un-isolate the set). We suppress the re-broadcast and flag
            # drove_viewer so applyResponse() leaves the view alone.
            drew_clearance = any(
                r.get("kind") == "clearance" and r.get("drew")
                for r in resolved
            )
            drove_viewer = _drove_viewer(resolved)
            if not drew_clearance and not drove_viewer:
                await _broadcast(list(highlighted.keys()))
            _remember(highlighted, resolved)
            log.debug(
                "agent_done total=%.2fs llm_calls=%d llm=%.2fs tools=%.2fs "
                "(narration skipped)",
                time.perf_counter() - t_start, llm_calls, llm_seconds,
                tool_seconds,
            )
            return {
                "reply": _compose_reply(resolved),
                "elements": list(highlighted.values()),
                "drew_clearance": drew_clearance,
                "drove_viewer": drove_viewer,
            }

    # Ran out of turns: still show whatever was resolved.
    await _broadcast(list(highlighted.keys()))
    _remember(highlighted, resolved)
    log.info(
        "agent_exhausted total=%.2fs llm_calls=%d llm=%.2fs tools=%.2fs",
        time.perf_counter() - t_start, llm_calls, llm_seconds, tool_seconds,
    )
    return {
        "reply": "I couldn't finish that within the allowed number of steps.",
        "elements": list(highlighted.values()),
    }


async def _run_tool(name: str, args: dict, highlighted: dict,
                    resolved: list, tool_error: list) -> dict:
    """Dispatch one tool call. SQL runs off the event loop.

    A find tool's query can fail (most commonly an expired Databricks token).
    When that happens we record a plain-language message in tool_error so the
    route can surface it as an honest error instead of a fake success line.
    """
    try:
        if name == "find_by_category":
            data = await asyncio.to_thread(
                _find_by_category,
                args.get("category"),
                args.get("level"),
                args.get("room_name"),
                args.get("limit", 500),
            )
            _collect(highlighted, data.get("elements", []))
            costs = [e.get("replacement_cost") for e in data.get("elements", [])
                     if isinstance(e.get("replacement_cost"), (int, float))]
            resolved.append({
                "kind": "category",
                "category": args.get("category"),
                "level": args.get("level"),
                "room_name": args.get("room_name"),
                "count": data.get("count", 0),
                "cost_min": min(costs) if costs else None,
                "cost_max": max(costs) if costs else None,
            })
            return _summary(data)
        if name == "find_elements":
            data = await asyncio.to_thread(
                _find_elements,
                args.get("metric"),
                args.get("op"),
                args.get("value", 0.0),
                args.get("limit", 50),
            )
            _collect(highlighted, data.get("elements", []))
            els = data.get("elements", [])
            top = els[0] if els else {}
            resolved.append({
                "kind": "cost",
                "metric": args.get("metric"),
                "op": args.get("op"),
                "value": args.get("value", 0.0),
                "count": data.get("count", 0),
                "top_asset_type": top.get("asset_type"),
                "top_description": top.get("description"),
                "top_metric_value": top.get("metric_value"),
            })
            return _summary(data)
        if name == "find_workorders":
            data = await asyncio.to_thread(
                _find_workorders,
                args.get("level"),
                args.get("status"),
                args.get("priority"),
                args.get("failure_class"),
                args.get("asset_category"),
                args.get("reported_by_role"),
                args.get("measure", "count"),
            )
            resolved.append({"kind": "workorders", **data})
            return data
        if name == "find_in_room":
            data = await _find_in_room(
                args.get("room_number"),
                args.get("room_external_id"),
                args.get("category"),
                args.get("limit", 50),
            )
            _collect(highlighted, data.get("elements", []))
            room = data.get("room", {})
            resolved.append({
                "kind": "in_room",
                "room_name": room.get("room_name"),
                "room_number": room.get("room_number"),
                "level": room.get("level_name"),
                "category": args.get("category"),
                "count": data.get("count", 0),
            })
            return _summary(data)
        if name == "find_extreme":
            data = await _find_spatial(
                args.get("op"),
                args.get("category"),
                args.get("level"),
                None,
                args.get("limit", 1),
            )
            _collect(highlighted, data.get("elements", []))
            els = data.get("elements", [])
            top = els[0] if els else {}
            resolved.append({
                "kind": "extreme",
                "op": args.get("op"),
                "category": args.get("category"),
                "level": args.get("level"),
                "count": data.get("count", 0),
                "top_asset_type": top.get("asset_type"),
                "top_description": top.get("description"),
                "top_volume_cuft": top.get("volume_cuft"),
            })
            return _summary(data)
        if name == "find_nearest":
            data = await _find_spatial(
                "nearest",
                args.get("category"),
                args.get("level"),
                args.get("reference_external_id"),
                args.get("limit", 5),
            )
            _collect(highlighted, data.get("elements", []))
            els = data.get("elements", [])
            top = els[0] if els else {}
            resolved.append({
                "kind": "nearest",
                "category": args.get("category"),
                "level": args.get("level"),
                "count": data.get("count", 0),
                "top_asset_type": top.get("asset_type"),
                "top_description": top.get("description"),
                "top_distance_ft": top.get("distance_ft"),
            })
            return _summary(data)
        if name == "find_clearance":
            try:
                data = await _find_clearance(
                    args.get("external_id"), args.get("description")
                )
            except HTTPException as exc:
                # Resolution problems (no match / ambiguous / bad id) are not
                # data-warehouse failures \u2014 hand the model a short, usable
                # note so it can ask the user to clarify instead of the turn
                # dying with a generic "data query failed".
                detail = getattr(exc, "detail", "") or ""
                status = getattr(exc, "status_code", 0)
                if status in (400, 404, 409):
                    return {"clearance_unresolved": str(detail)}
                raise
            # Draw the clearance zones + intruders in the viewer. The backend
            # returns a flat `draw` payload the viewer's draw_clearance command
            # consumes directly (panel id, zone boxes, intruder ids). The
            # draw_clearance command ALREADY highlights the panel and tints the
            # intruders against the zone boxes, so we must NOT also _collect the
            # intruders here \u2014 doing so triggers the loop's end-of-turn
            # highlight_elements broadcast, which isolate()s just those ids and
            # hides the building the clearance boxes sit against (the boxes then
            # appear to vanish, leaving only orange intruders). Keep the panel
            # in the result list so it shows in the sidebar, nothing more.
            draw = data.get("draw")
            if isinstance(draw, dict) and draw.get("zones"):
                await _broadcast_command("draw_clearance", draw)
                pid = data.get("external_id")
                if pid and pid not in highlighted:
                    highlighted[pid] = {
                        "external_id": pid,
                        "label": _label({
                            "asset_type": data.get("asset_type"),
                            "description": data.get("description"),
                        }),
                        "fields": {
                            "asset_type": data.get("asset_type"),
                            "description": data.get("description"),
                        },
                    }
                resolved.append({"kind": "clearance", "drew": True, **{
                    k: data.get(k) for k in ("clear", "mode", "rule")
                    if k in data
                }})
            else:
                resolved.append({"kind": "clearance", "drew": False, **{
                    k: data.get(k) for k in ("clear", "mode", "rule")
                    if k in data
                }})
            return data
        if name == "highlight_elements":
            ids = [i for i in (args.get("external_ids") or [])
                   if isinstance(i, str)]
            return {"applied": True, "accepted": len(ids)}
        if name == "highlight_rooms":
            ids = [i for i in (args.get("space_external_ids") or [])
                   if isinstance(i, str)]
            await _broadcast_command(
                "highlight_rooms", {"space_external_ids": ids}
            )
            resolved.append({"kind": "act", "command": "highlight_rooms",
                             "count": len(ids)})
            return {"applied": True, "accepted": len(ids)}
        if name == "isolate":
            ids = [i for i in (args.get("external_ids") or [])
                   if isinstance(i, str)]
            await _broadcast_command("isolate", {"external_ids": ids})
            resolved.append({"kind": "act", "command": "isolate",
                             "count": len(ids)})
            return {"applied": True, "accepted": len(ids)}
        if name == "zoom_to":
            cmd_args: dict = {}
            space = args.get("space_external_id")
            if isinstance(space, str) and space:
                cmd_args["space_external_id"] = space
            else:
                ids = [i for i in (args.get("external_ids") or [])
                       if isinstance(i, str)]
                cmd_args["external_ids"] = ids
            await _broadcast_command("zoom_to", cmd_args)
            resolved.append({"kind": "act", "command": "zoom_to"})
            return {"applied": True}
        if name == "reset":
            await _broadcast_command("reset", {})
            resolved.append({"kind": "act", "command": "reset"})
            return {"applied": True}
        return {"error": f"unknown tool {name}"}
    except Exception as exc:
        # A real failure (not a valid empty result). Record a human-readable
        # message for the reply, and still hand the model the raw error.
        if name in ("find_by_category", "find_elements", "find_workorders",
                    "find_in_room", "find_extreme", "find_nearest",
                    "find_clearance"):
            tool_error.append(_query_error_message(exc))
        return {"error": str(exc)}


def _query_error_message(exc: Exception) -> str:
    """Translate a data-query failure into a plain, demo-safe sentence."""
    text = str(exc).lower()
    if "403" in text or "forbidden" in text or "token" in text:
        return (
            "I couldn't reach the data \u2014 the Databricks connection was "
            "rejected (403 Forbidden), which usually means the access token "
            "has expired. Restart the backend with a fresh token and try again."
        )
    if "502" in text or "connection" in text or "connect" in text:
        return (
            "I couldn't reach the data warehouse (connection failed). "
            "Check that the backend can reach Databricks and try again."
        )
    return f"The data query failed: {exc}"


def _collect(highlighted: dict, elements: list) -> None:
    """Remember every resolved element so the viewer can list/highlight it."""
    for e in elements:
        ext = e.get("external_id") if isinstance(e, dict) else None
        if ext and ext not in highlighted:
            highlighted[ext] = {
                "external_id": ext,
                "label": _label(e),
                "fields": dict(e),
            }


def _label(e: dict) -> str | None:
    parts = [str(e[k]) for k in ("asset_type", "description")
             if e.get(k)]
    return " \u2013 ".join(parts) or None


_CATEGORY_WORDS = {
    "air_terminals": "air terminals",
    "supply_diffusers": "supply diffusers",
    "return_grilles": "return grilles",
    "exhaust_grilles": "exhaust grilles",
    "heat_pumps": "water-source heat pumps",
    "cooling_tower": "cooling tower",
    "air_handlers": "air handlers",
    "fans": "fans",
    "transformers": "transformers",
    "electrical_panels": "electrical panels",
    "water_heaters": "water heaters",
    "sprinklers": "sprinklers",
    "lighting": "lighting fixtures",
    "receptacles": "receptacles",
    "plumbing_fixtures": "plumbing fixtures",
}

_METRIC_WORDS = {
    "replacement_cost": "replacement cost",
    "maintenance_cost": "maintenance cost",
}

# asset_type (as stored) -> a readable singular noun for the one-line reply.
_ASSET_TYPE_WORDS = {
    "COOLING_TOWER": "cooling tower",
    "AHU": "air handler",
    "AIR_HANDLER": "air handler",
    "TRANSFORMER": "transformer",
    "ELECTRICAL_PANEL": "electrical panel",
    "WATER_HEATER": "water heater",
    "WSHP": "water-source heat pump",
    "HEAT_PUMP": "heat pump",
    "FAN": "fan",
    "PUMP": "pump",
    "LIGHTING_FIXTURE": "lighting fixture",
    "RECEPTACLE": "receptacle",
    "SPRINKLER": "sprinkler",
    "PLUMBING_FIXTURE": "plumbing fixture",
    "AIR_TERMINAL": "air terminal",
    "SUPPLY_DIFFUSER": "supply diffuser",
    "RETURN_GRILLE": "return grille",
    "EXHAUST_GRILLE": "exhaust grille",
}


def _asset_noun(asset_type: str | None) -> str:
    if not asset_type:
        return "asset"
    return _ASSET_TYPE_WORDS.get(
        asset_type, asset_type.replace("_", " ").lower()
    )


def _article(noun: str) -> str:
    """'a' / 'an' for a noun (by its first letter)."""
    return "an" if noun[:1].lower() in "aeiou" else "a"


def _cost_phrase(r: dict) -> str:
    """One readable clause for a cost resolution (name + price when a single)."""
    n = r.get("count", 0)
    metric = _METRIC_WORDS.get(r.get("metric"), "cost")
    op = r.get("op")
    if op in ("top", "bottom"):
        superlative = "most" if op == "top" else "least"
        if n == 1:
            noun = _asset_noun(r.get("top_asset_type"))
            art = "an" if noun[:1] in "aeiou" else "a"
            val = r.get("top_metric_value")
            price = f" ({metric} ${val:,.0f})" if val is not None else ""
            return f"the {superlative} expensive asset, {art} {noun}{price}"
        order = "highest" if op == "top" else "lowest"
        return f"the {n} {order}-{metric} assets"
    sym = {"gt": "over", "gte": "at or over",
           "lt": "under", "lte": "at or under"}.get(op, "matching")
    val = r.get("value", 0)
    noun = "asset" if n == 1 else "assets"
    return f"{n} {noun} with {metric} {sym} ${val:,.0f}"


def _compose_reply(resolved: list[dict]) -> str:
    """Write the one-line answer ourselves (no extra LLM round-trip).

    Summarizes what the find tool(s) resolved and were highlighted, in plain
    language, matching the agent's old narration without the latency cost.
    """
    if not resolved:
        return "Highlighted the matching elements in the viewer."
    parts = []
    for r in resolved:
        n = r.get("count", 0)
        if r.get("kind") == "category":
            word = _CATEGORY_WORDS.get(r.get("category"), "elements")
            if n == 1:
                phrase = f"the {word}" if word == "cooling tower" else f"1 {word}"
            else:
                phrase = f"{n} {word}"
            where = ""
            if r.get("level"):
                where += f" on {r['level']}"
            if r.get("room_name"):
                where += f" in rooms matching '{r['room_name']}'"
            lo = r.get("cost_min")
            hi = r.get("cost_max")
            cost = ""
            if lo is not None and hi is not None:
                if lo == hi:
                    cost = f" (replacement cost ${lo:,.0f} each)"
                else:
                    cost = f" (replacement cost ${lo:,.0f}\u2013${hi:,.0f} each)"
            parts.append(f"{phrase}{where}{cost}")
        elif r.get("kind") == "in_room":
            word = _CATEGORY_WORDS.get(r.get("category"), "assets")
            noun = word if r.get("category") else "assets"
            where = ""
            if r.get("room_number"):
                where = f" in room {r['room_number']}"
            elif r.get("room_name"):
                where = f" in {r['room_name']}"
            parts.append(f"{n} {noun}{where}")
        elif r.get("kind") in ("extreme", "nearest"):
            noun = _asset_noun(r.get("top_asset_type"))
            if r.get("kind") == "extreme":
                op = r.get("op") or "matching"
                lead = f"the {op} element, {_article(noun)} {noun}"
            else:
                lead = f"the nearest {noun}" if n == 1 else \
                    f"the {n} nearest elements"
            parts.append(lead)
        elif r.get("kind") == "clearance":
            clear = r.get("clear")
            if clear is True:
                parts.append("the panel's working clearance is clear")
            elif clear is False:
                parts.append("the panel's working clearance is obstructed")
            else:
                parts.append("the clearance check")
        elif r.get("kind") == "act":
            cmd = r.get("command")
            act_words = {
                "highlight_rooms": "Highlighted the rooms",
                "isolate": "Isolated the selected elements",
                "zoom_to": "Zoomed to the selection",
                "reset": "Reset the view",
            }
            parts.append(act_words.get(cmd, "Updated the view"))
        else:
            parts.append(_cost_phrase(r))
    # If every resolution is an act (view-driving) command, those phrases are
    # already complete sentences \u2014 don't wrap them in "Highlighted ... in
    # the viewer." Same for a clearance-only answer, which is a yes/no finding,
    # not a highlight. Otherwise wrap the find summaries as before.
    if all(r.get("kind") == "act" for r in resolved):
        return ". ".join(parts) + "."
    if all(r.get("kind") == "clearance" for r in resolved):
        sentences = [p[:1].upper() + p[1:] for p in parts]
        return ". ".join(sentences) + "."
    body = "; ".join(parts)
    return f"Highlighted {body} in the viewer."


def _summary(data: dict) -> dict:
    """A small, id-light summary for the model.

    The find tools highlight their own result, so the model does NOT need the
    external_ids and we deliberately do not include them here \u2014 keeping the
    id list out of the model's context is what makes a large (100+ element)
    selection as fast as a small one.

    For a SMALL result (<= SMALL_RESULT) we include each element's descriptive
    attributes (type, description, level, room, cost, manufacturer, model,
    serial) \u2014 still no ids \u2014 so the model can answer an attribute
    question like "what model/serial is the cooling tower?" from real data. A
    large result stays id-light and attribute-light to keep it fast.
    """
    elements = data.get("elements", [])
    count = data.get("count", len(elements))
    out = {
        "count": count,
        "highlighted": True,
        "note": "These elements are now highlighted in the viewer.",
    }
    if count and count <= SMALL_RESULT:
        out["elements"] = [_element_facts(e) for e in elements]
    return out


# Attribute fields surfaced to the model for a small result, keyed as the
# model should read them. external_id is deliberately omitted (speed + the
# viewer already has it).
_FACT_FIELDS = (
    "asset_type",
    "description",
    "level_name",
    "room_name",
    "replacement_cost",
    "metric_value",
    "manufacturer",
    "model_number",
    "serial_number",
)


def _element_facts(e: dict) -> dict:
    """Pull just the descriptive, id-free fields for one element."""
    if not isinstance(e, dict):
        return {}
    return {k: e.get(k) for k in _FACT_FIELDS if e.get(k) is not None}



async def _broadcast(external_ids: list[str]) -> None:
    """Drive every connected viewer via the existing command hub."""
    if not external_ids:
        return
    app = _app()
    await app.hub.broadcast({
        "command": "highlight_elements",
        "args": {"external_ids": external_ids},
    })


async def _broadcast_command(command: str, cmd_args: dict) -> None:
    """Drive every connected viewer with any allow-listed viewer command.

    Used by the act tools (highlight_rooms / isolate / zoom_to / reset), which
    broadcast one command the viewer reacts to. The command name is validated
    against the app's fixed ALLOWED_COMMANDS before it reaches the hub.
    """
    app = _app()
    if command not in app.ALLOWED_COMMANDS:
        raise ValueError(f"command not allowed: {command}")
    await app.hub.broadcast({"command": command, "args": cmd_args})
