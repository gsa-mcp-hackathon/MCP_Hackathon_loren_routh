"""Tests for the viewer-tool argument validation and the POST boundary."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from bim_viewer_mcp import utils
from bim_viewer_mcp.models import (
    ALLOWED_DIRECTIONS,
    ALLOWED_METRICS,
    ALLOWED_OPS,
    ALLOWED_SIZE_OPS,
    MAX_IDS,
    MAX_INROOM_LIMIT,
    MAX_QUERY_LIMIT,
    MAX_SPATIAL_LIMIT,
    sanitize_external_ids,
    valid_external_id,
)

GOOD_ID = "7485c179-9364-48d7-b68b-6736718012c4-0005fbe8"
GOOD_ID_2 = "a1b2c3d4-0000-1111-2222-333344445555-0000abcd"


@pytest.mark.parametrize(
    "value",
    [
        GOOD_ID,
        GOOD_ID.upper(),
    ],
)
def test_valid_external_id_accepts_well_formed(value: str) -> None:
    assert valid_external_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-an-id",
        "7485c179-9364-48d7-b68b-6736718012c4",  # missing trailing segment
        "7485c179-9364-48d7-b68b-6736718012c4-0005fbe8-extra",
        "zzzzzzzz-9364-48d7-b68b-6736718012c4-0005fbe8",  # non-hex
        1737,
        None,
        {"external_id": GOOD_ID},
    ],
)
def test_valid_external_id_rejects_bad(value: object) -> None:
    assert not valid_external_id(value)


def test_sanitize_dedupes_and_drops_invalid() -> None:
    result = sanitize_external_ids(
        [GOOD_ID, "bad", GOOD_ID, GOOD_ID_2, 42, None]
    )
    assert result == [GOOD_ID, GOOD_ID_2]


def test_sanitize_caps_at_max_ids() -> None:
    many = [
        f"{i:08x}-9364-48d7-b68b-6736718012c4-0005fbe8"
        for i in range(MAX_IDS + 50)
    ]
    assert len(sanitize_external_ids(many)) == MAX_IDS


def test_sanitize_non_list_returns_empty() -> None:
    assert sanitize_external_ids("string") == []
    assert sanitize_external_ids(None) == []


class _FakeClient:
    """Capture client policy and the posted body; return a controlled response."""

    init_kwargs: dict[str, Any] = {}
    posted: dict[str, Any] = {}
    response = httpx.Response(200, json={"applied": True})

    def __init__(self, **kwargs: Any) -> None:
        type(self).init_kwargs = kwargs

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    class _Stream:
        def __init__(self, response: httpx.Response) -> None:
            self.response = response

        async def __aenter__(self) -> httpx.Response:
            return self.response

        async def __aexit__(self, *args: object) -> None:
            return None

    def stream(self, method: str, path: str, **kwargs: Any) -> "_FakeClient._Stream":
        type(self).posted = {"method": method, "path": path, "json": kwargs.get("json")}
        self.response.request = httpx.Request(method, f"http://viewer/{path}")
        return self._Stream(self.response)


@pytest.mark.asyncio
async def test_post_json_uses_safe_client_policy(monkeypatch) -> None:
    monkeypatch.setattr(utils.httpx, "AsyncClient", _FakeClient)
    out = await utils.post_json(
        "http://localhost:8000",
        "api/viewer/command",
        {"command": "reset", "args": {}},
    )
    assert out == {"applied": True}
    assert _FakeClient.init_kwargs["base_url"] == "http://localhost:8000"
    assert _FakeClient.init_kwargs["follow_redirects"] is False
    assert _FakeClient.init_kwargs["trust_env"] is False
    assert _FakeClient.posted["method"] == "POST"
    assert _FakeClient.posted["json"] == {"command": "reset", "args": {}}


@pytest.mark.asyncio
async def test_post_json_rejects_escaping_path(monkeypatch) -> None:
    class UnexpectedClient:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("HTTP client must not be created for rejected input")

    monkeypatch.setattr(utils.httpx, "AsyncClient", UnexpectedClient)
    with pytest.raises(ValueError, match="relative API path"):
        await utils.post_json("http://localhost:8000", "../admin", {})


@pytest.mark.asyncio
async def test_post_json_sanitizes_upstream_error(monkeypatch) -> None:
    sensitive = "token=secret SSN 123-45-6789"
    _FakeClient.response = httpx.Response(500, text=sensitive)
    monkeypatch.setattr(utils.httpx, "AsyncClient", _FakeClient)
    with pytest.raises(utils.UpstreamServiceError) as captured:
        await utils.post_json("http://localhost:8000", "api/viewer/command", {})
    assert str(captured.value) == "Upstream service returned status 500."
    assert sensitive not in str(captured.value)


# ── find_elements cost tool ───────────────────────────────────────────────────────────


def test_find_elements_allowlists_are_closed() -> None:
    """The metric/op allowlists stay small so no free-form query can slip in."""
    assert ALLOWED_METRICS == {"replacement_cost", "maintenance_cost"}
    assert ALLOWED_OPS == {"top", "gt", "gte", "lt", "lte"}
    assert MAX_QUERY_LIMIT == 500


async def _get_find_elements():
    from bim_viewer_mcp.app import mcp

    tool = await mcp.get_tool("find_elements")
    return tool.fn


@pytest.mark.asyncio
async def test_find_elements_posts_bounded_allowlisted_query(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_post_json(base_url, path, payload, *args, **kwargs):
        captured["base_url"] = base_url
        captured["path"] = path
        captured["payload"] = payload
        return {
            "metric": "replacement_cost",
            "elements": [
                {
                    "external_id": GOOD_ID,
                    "asset_type": "AHU",
                    "description": "Air handler",
                    "metric_value": 180000,
                },
                {  # dropped: malformed external_id
                    "external_id": "not-an-id",
                    "asset_type": "X",
                    "metric_value": 1,
                },
            ],
        }

    import bim_viewer_mcp.tools.find_elements as fe_mod

    monkeypatch.setattr(fe_mod, "post_json", fake_post_json)
    fn = await _get_find_elements()

    out = await fn(metric="replacement_cost", op="top", limit=10000)

    assert captured["path"] == "api/assets/query"
    assert captured["payload"]["metric"] == "replacement_cost"
    assert captured["payload"]["op"] == "top"
    assert captured["payload"]["limit"] == MAX_QUERY_LIMIT  # clamped
    assert out["count"] == 1
    assert out["elements"][0]["external_id"] == GOOD_ID


@pytest.mark.asyncio
async def test_find_elements_returns_empty_on_backend_failure(monkeypatch) -> None:
    async def failing_post_json(*args, **kwargs):
        raise utils.UpstreamServiceError("down")

    import bim_viewer_mcp.tools.find_elements as fe_mod

    monkeypatch.setattr(fe_mod, "post_json", failing_post_json)
    fn = await _get_find_elements()

    out = await fn(metric="maintenance_cost", op="gt", value=50000.0)
    assert out["count"] == 0
    assert out["elements"] == []


# ── spatial tools (find_nearest / find_extreme / find_in_room) ────────────────────────


def test_spatial_allowlists_are_closed() -> None:
    """The spatial op allowlists stay small and the limits are bounded."""
    assert ALLOWED_DIRECTIONS == {
        "northmost",
        "southmost",
        "eastmost",
        "westmost",
    }
    assert ALLOWED_SIZE_OPS == {"largest", "smallest"}
    assert MAX_SPATIAL_LIMIT == 50
    assert MAX_INROOM_LIMIT == 500


async def _get_tool_fn(name: str):
    from bim_viewer_mcp.app import mcp

    tool = await mcp.get_tool(name)
    return tool.fn


@pytest.mark.asyncio
async def test_find_nearest_posts_nearest_op_and_bounds_limit(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_post_json(base_url, path, payload, *args, **kwargs):
        captured["path"] = path
        captured["payload"] = payload
        return {
            "op": "nearest",
            "elements": [
                {
                    "external_id": GOOD_ID,
                    "asset_type": "TRANSFORMER",
                    "description": "Transformer",
                    "level_name": "Level 1",
                    "distance_ft": 4.2,
                },
                {  # dropped: malformed id
                    "external_id": "nope",
                    "distance_ft": 9.9,
                },
            ],
        }

    import bim_viewer_mcp.tools.find_nearest as fn_mod

    monkeypatch.setattr(fn_mod, "post_json", fake_post_json)
    fn = await _get_tool_fn("find_nearest")

    out = await fn(
        reference_external_id=GOOD_ID,
        category="transformers",
        limit=10000,
    )

    assert captured["path"] == "api/assets/spatial"
    assert captured["payload"]["op"] == "nearest"
    assert captured["payload"]["reference_external_id"] == GOOD_ID
    assert captured["payload"]["category"] == "transformers"
    assert captured["payload"]["limit"] == MAX_SPATIAL_LIMIT  # clamped
    assert out["count"] == 1
    assert out["elements"][0]["distance_ft"] == 4.2


@pytest.mark.asyncio
async def test_find_nearest_rejects_bad_reference_id(monkeypatch) -> None:
    async def unexpected_post_json(*args, **kwargs):
        raise AssertionError("must not POST for an invalid reference id")

    import bim_viewer_mcp.tools.find_nearest as fn_mod

    monkeypatch.setattr(fn_mod, "post_json", unexpected_post_json)
    fn = await _get_tool_fn("find_nearest")

    out = await fn(reference_external_id="not-an-id")
    assert out["count"] == 0
    assert out["elements"] == []


@pytest.mark.asyncio
async def test_find_extreme_size_keeps_volume_direction_drops_it(monkeypatch) -> None:
    async def fake_post_json(base_url, path, payload, *args, **kwargs):
        # Echo back a shape appropriate to the op.
        if payload["op"] in ALLOWED_SIZE_OPS:
            return {
                "op": payload["op"],
                "elements": [
                    {
                        "external_id": GOOD_ID,
                        "asset_type": "COOLING_TOWER",
                        "description": "Cooling tower",
                        "level_name": "Roof Level",
                        "volume_cuft": 2751.0,
                    }
                ],
            }
        return {
            "op": payload["op"],
            "elements": [
                {
                    "external_id": GOOD_ID_2,
                    "asset_type": "AHU",
                    "description": "Air handler",
                    "level_name": "Level 2",
                }
            ],
        }

    import bim_viewer_mcp.tools.find_extreme as fx_mod

    monkeypatch.setattr(fx_mod, "post_json", fake_post_json)
    fn = await _get_tool_fn("find_extreme")

    big = await fn(op="largest")
    assert big["count"] == 1
    assert big["elements"][0]["volume_cuft"] == 2751.0

    north = await fn(op="northmost", category="air_handlers")
    assert north["count"] == 1
    assert "volume_cuft" not in north["elements"][0]


@pytest.mark.asyncio
async def test_find_in_room_requires_an_identifier(monkeypatch) -> None:
    async def unexpected_post_json(*args, **kwargs):
        raise AssertionError("must not POST without a room identifier")

    import bim_viewer_mcp.tools.find_in_room as ir_mod

    monkeypatch.setattr(ir_mod, "post_json", unexpected_post_json)
    fn = await _get_tool_fn("find_in_room")

    out = await fn()
    assert out["count"] == 0
    assert out["elements"] == []


@pytest.mark.asyncio
async def test_find_in_room_posts_room_number(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_post_json(base_url, path, payload, *args, **kwargs):
        captured["path"] = path
        captured["payload"] = payload
        return {
            "room": {
                "external_id": GOOD_ID,
                "room_name": "EDP I 54",
                "room_number": "54",
                "level_name": "Level 2",
            },
            "count": 1,
            "elements": [
                {
                    "external_id": GOOD_ID_2,
                    "asset_type": "DUPLEX_RECEPTACLE",
                    "description": "Receptacle",
                    "level_name": "Level 2",
                }
            ],
        }

    import bim_viewer_mcp.tools.find_in_room as ir_mod

    monkeypatch.setattr(ir_mod, "post_json", fake_post_json)
    fn = await _get_tool_fn("find_in_room")

    out = await fn(room_number="54", category="receptacles", limit=10000)
    assert captured["path"] == "api/assets/in-room"
    assert captured["payload"]["room_number"] == "54"
    assert captured["payload"]["category"] == "receptacles"
    assert captured["payload"]["limit"] == MAX_INROOM_LIMIT  # clamped
    assert out["count"] == 1
    assert out["room"]["room_number"] == "54"


# ── find_clearance tool ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_clearance_passes_through_verdict(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_post_json(base_url, path, payload, *args, **kwargs):
        captured["path"] = path
        captured["payload"] = payload
        return {
            "external_id": GOOD_ID,
            "asset_type": "ELECTRICAL_PANEL",
            "description": "Panelboard",
            "mode": "directional",
            "rule": "NEC 110.26(A) working clearance",
            "clear": False,
            "intruders": [
                {
                    "external_id": GOOD_ID_2,
                    "asset_type": "STRUCTURAL_COLUMN",
                    "description": "Column",
                },
                {"external_id": "bad", "asset_type": "X"},  # dropped
            ],
            "draw": {"panel_external_id": GOOD_ID, "zones": []},
        }

    import bim_viewer_mcp.tools.find_clearance as fc_mod

    monkeypatch.setattr(fc_mod, "post_json", fake_post_json)
    fn = await _get_tool_fn("find_clearance")

    out = await fn(external_id=GOOD_ID)
    assert captured["path"] == "api/assets/clearance"
    assert captured["payload"] == {
        "category": "electrical_panels",
        "external_id": GOOD_ID,
    }
    assert out["clear"] is False
    assert len(out["intruders"]) == 1
    assert out["intruders"][0]["external_id"] == GOOD_ID_2
    assert out["draw"]["panel_external_id"] == GOOD_ID


@pytest.mark.asyncio
async def test_find_clearance_rejects_bad_id(monkeypatch) -> None:
    async def unexpected_post_json(*args, **kwargs):
        raise AssertionError("must not POST for an invalid panel id")

    import bim_viewer_mcp.tools.find_clearance as fc_mod

    monkeypatch.setattr(fc_mod, "post_json", unexpected_post_json)
    fn = await _get_tool_fn("find_clearance")

    out = await fn(external_id="not-an-id")
    assert out["clear"] is None
