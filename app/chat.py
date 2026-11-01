"""Chat box backend: a model (Databricks or USAi) calls MCP tools, and the viewer highlights the result."""
import json
import os
import uuid

from fastapi import APIRouter, HTTPException
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from openai import AsyncOpenAI
from pydantic import BaseModel

from db import cfg
from fields import build_colors, field_names, select_elements
from toolname import assert_tool_names_valid

# ---------------------------------------------------------------- MCP tools
# The model only sees small summaries. The full list of IDs and colors goes to the viewer through a token.
mcp = FastMCP("bim-tools")
_VIEW = {}  # view_token -> payload for the viewer


def stash(payload):
    token = uuid.uuid4().hex
    _VIEW[token] = payload
    while len(_VIEW) > 50:
        _VIEW.pop(next(iter(_VIEW)))
    return token


FILTER_HELP = ('filters maps a field name to its allowed values, for example {"level": ["F003"]}. '
               "Valid fields: " + ", ".join(field_names()) + ".")


@mcp.tool(description="Highlight the elements that match ALL the filters in the viewer; everything else fades. "
                      + FILTER_HELP)
def highlight_elements(filters: dict[str, list[str]]) -> dict:
    try:
        rows = select_elements([], filters)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    ids = [r["UNIQUE_ID"] for r in rows]
    if not ids:  # help the model recover by showing which values exist
        hint = {f: sorted({str(r[f.upper()]) for r in select_elements([f], {})})[:30] for f in filters}
        return {"count": 0, "valid_values": hint}
    return {"count": len(ids), "sample_ids": ids[:5], "view_token": stash({"highlight": ids})}


@mcp.tool(description="Color every element by one field (text fields get one color per value, numeric fields a "
                      "gradient) and show a legend. Optional filters, in the same format as highlight_elements, "
                      "limit which elements are colored; the rest turn gray. Valid fields: "
                      + ", ".join(field_names()) + ".")
def color_by(field: str, filters: dict[str, list[str]] | None = None) -> dict:
    try:
        colors, legend, kind = build_colors(field, filters or {})
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return {"field": field, "kind": kind, "elements": len(colors), "legend": legend,
            "view_token": stash({"colors": colors, "legend": legend})}


@mcp.tool
def reset_view() -> dict:
    """Clear all highlighting and coloring in the viewer."""
    return {"reset": True, "view_token": stash({"reset": True})}


# ---------------------------------------------------------------- model providers

def configured_providers():
    found = []
    if os.getenv("DATABRICKS_MODEL"):
        found.append("databricks")
    if os.getenv("USAI_BASE_URL") and os.getenv("USAI_API_KEY") and os.getenv("USAI_MODEL"):
        found.append("usai")
    return found


def get_llm(provider):
    """Both services speak the OpenAI Chat Completions protocol, so only the URL, key and model differ."""
    if provider == "databricks":
        token = cfg.authenticate()["Authorization"].split(" ", 1)[1]  # fresh token per request (they expire)
        return AsyncOpenAI(api_key=token, base_url=f"{cfg.host.rstrip('/')}/serving-endpoints"), os.environ["DATABRICKS_MODEL"]
    if provider == "usai":
        # USAI_BASE_URL ends in /api/v1: the client appends /chat/completions
        return AsyncOpenAI(api_key=os.environ["USAI_API_KEY"], base_url=os.environ["USAI_BASE_URL"]), os.environ["USAI_MODEL"]
    raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")


# ---------------------------------------------------------------- the chat loop

SYSTEM = (
    "You help people explore a building model in a 3D viewer. To show things, call highlight_elements "
    "or color_by; call reset_view to clear. Use only field names and values the tools report, and never "
    "invent IDs. All lengths are in feet. Keep answers short."
)
router = APIRouter()


class ChatIn(BaseModel):
    messages: list[dict]  # prior turns: {"role": "user"|"assistant", "content": str}
    provider: str = "databricks"


@router.get("/api/providers")
def providers():
    return configured_providers()


@router.post("/api/chat")
async def chat(body: ChatIn):
    if body.provider not in configured_providers():
        raise HTTPException(status_code=400, detail=f"Provider not configured: {body.provider}")
    llm, model = get_llm(body.provider)
    messages = [{"role": "system", "content": SYSTEM}] + body.messages
    view = {}
    async with Client(mcp) as mcp_client:  # in-memory connection to the tools above
        raw_tools = await mcp_client.list_tools()
        assert_tool_names_valid([t.name for t in raw_tools])  # Claude rejects names outside [a-zA-Z0-9_-]
        tools = [{"type": "function",
                  "function": {"name": t.name, "description": t.description or "", "parameters": t.inputSchema}}
                 for t in raw_tools]
        for _ in range(6):  # at most six model turns per question
            try:
                reply = (await llm.chat.completions.create(model=model, messages=messages, tools=tools)).choices[0].message
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Model call failed: {exc}") from exc
            if not reply.tool_calls:
                return {"reply": reply.content or "", "view": view}
            messages.append(reply.model_dump(exclude_none=True))
            for call in reply.tool_calls:
                try:
                    result = await mcp_client.call_tool(call.function.name, json.loads(call.function.arguments or "{}"))
                    text = "".join(c.text for c in result.content if hasattr(c, "text"))
                except Exception as exc:  # let the model see the error and recover
                    text = f"Tool error: {exc}"
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("view_token"):
                    payload = _VIEW.pop(parsed["view_token"], {})
                    if payload.get("reset"):
                        view = {}  # a reset wipes whatever earlier calls set
                    view.update(payload)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
    return {"reply": "I couldn't finish that within the allowed number of steps.", "view": view}
