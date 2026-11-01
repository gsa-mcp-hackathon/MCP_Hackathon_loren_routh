"""Backend Role A: MCP client to the Databricks Genie Agent.

The viewer posts a plain-language question to ``/api/chat``. This module asks
the Genie Agent over its MCP endpoint, waits for the query to finish, and shapes
the result into the viewer contract::

    {reply, elements, rooms}

``elements`` are the model elements to highlight (keyed by ``external_id``), and
``rooms`` carry the geometry needed to draw translucent room volumes.
"""
import json
import os
import subprocess
import time

import requests

from db import query_rows

# ---------------------------------------------------------------- configuration

# Known defaults for the demo; every value can be overridden by the environment
# so the same code runs locally (CLI token) and inside a Databricks App (env).
DEFAULT_HOST = "https://dbc-aae0ba16-8727.cloud.databricks.com"
DEFAULT_SPACE_ID = "01f1d0d60cf41b52b81699d2b813ec45"
DEFAULT_CLI_PROFILE = "dbc-aae0ba16-8727"

# Table the room geometry is joined from.
ROOMS_TABLE = os.getenv(
    "MECH_ROOMS_TABLE", "users.loren_routh.mech_rooms"
)

MCP_PROTOCOL_VERSION = "2025-06-18"

# Statuses that mean "still working"; anything else is terminal.
_IN_PROGRESS = {
    "EXECUTING_QUERY",
    "FETCHING_METADATA",
    "FILTERING_CONTEXT",
    "ASKING_AI",
    "PENDING_WAREHOUSE",
    "SUBMITTED",
}

# Column names (case-insensitive) that identify a highlightable element.
_EXTERNAL_ID_COLS = ("external_id", "unique_id", "source_unique_id")
# Column name that identifies a room to draw as a volume.
_SPACE_ID_COLS = ("space_external_id",)


def _host():
    return os.getenv("DATABRICKS_HOST", DEFAULT_HOST).rstrip("/")


def _space_id():
    return os.getenv("GENIE_SPACE_ID", DEFAULT_SPACE_ID)


def _cli_path():
    # Prefer the bundled CLI; allow an override for other machines.
    override = os.getenv("DATABRICKS_CLI_PATH")
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(
        here, "databricks_cli_1.21.0_windows_amd64", "databricks.exe"
    )


def get_token():
    """Return a bearer token for the Genie MCP endpoint.

    Prefers the local Databricks CLI (the proven path for the demo); falls back
    to ``DATABRICKS_TOKEN`` from the environment when the CLI is unavailable,
    which is the case inside a deployed Databricks App.
    """
    cli = _cli_path()
    profile = os.getenv("DATABRICKS_CONFIG_PROFILE", DEFAULT_CLI_PROFILE)
    if os.path.exists(cli):
        try:
            out = subprocess.run(
                [cli, "auth", "token", "--profile", profile],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            token = json.loads(out.stdout).get("access_token")
            if token:
                return token
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            pass  # fall through to the env var

    env_token = os.getenv("DATABRICKS_TOKEN")
    if env_token:
        return env_token

    raise RuntimeError(
        "No Databricks token: the CLI call failed and DATABRICKS_TOKEN is unset."
    )


# ---------------------------------------------------------------- MCP transport


class _GenieSession:
    """A short-lived JSON-RPC session over the Genie MCP streamable endpoint."""

    def __init__(self):
        self.url = f"{_host()}/api/2.0/mcp/genie/{_space_id()}"
        self.token = get_token()
        self.session_id = None
        self._rpc_id = 0
        self._http = requests.Session()

    def _headers(self):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _post(self, payload, capture_session=False):
        self._rpc_id += 1
        payload = {"jsonrpc": "2.0", "id": self._rpc_id, **payload}
        resp = self._http.post(
            self.url,
            headers=self._headers(),
            data=json.dumps(payload),
            timeout=60,
        )
        resp.raise_for_status()
        if capture_session:
            sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get(
                "mcp-session-id"
            )
            if sid:
                self.session_id = sid
        return _decode_jsonrpc(resp)

    def _notify(self, method):
        resp = self._http.post(
            self.url,
            headers=self._headers(),
            data=json.dumps({"jsonrpc": "2.0", "method": method}),
            timeout=60,
        )
        resp.raise_for_status()

    def initialize(self):
        self._post(
            {
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "bim-viewer", "version": "0.1"},
                },
            },
            capture_session=True,
        )
        self._notify("notifications/initialized")

    def call_tool(self, name, arguments):
        result = self._post(
            {
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return _structured_content(result)


def _decode_jsonrpc(resp):
    """Parse a JSON-RPC response that may arrive as JSON or SSE."""
    # The HTTP body is UTF-8; requests decodes it, so em-dashes etc. stay intact.
    ctype = resp.headers.get("Content-Type", "")
    if "text/event-stream" in ctype:
        # Concatenate the data: lines of the SSE frames and parse the JSON.
        chunks = []
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                chunks.append(line[len("data:"):].strip())
        text = "".join(chunks)
        return json.loads(text) if text else {}
    return resp.json()


def _structured_content(rpc_result):
    """Pull the Genie payload out of a tools/call JSON-RPC result."""
    result = rpc_result.get("result", {})
    sc = result.get("structuredContent")
    if sc is not None:
        return sc
    # Fallback: the same JSON is mirrored as a text content block.
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except (KeyError, ValueError):
                continue
    return {}


# ---------------------------------------------------------------- orchestration


def ask_genie(query, max_polls=40, poll_seconds=2):
    """Ask the Genie Agent and return its completed structured payload.

    Returns the ``content`` object with ``textAttachments`` and
    ``queryAttachments``. Raises on transport errors or a failed query.
    """
    space = _space_id()
    session = _GenieSession()
    session.initialize()

    started = session.call_tool(
        f"query_space_{space}", {"query": query}
    )
    conversation_id = started.get("conversationId")
    message_id = started.get("messageId")
    status = started.get("status")

    if status not in _IN_PROGRESS:
        # Rare: completed on the first call.
        return started.get("content", {}), status

    if not conversation_id or not message_id:
        raise RuntimeError(
            f"Genie did not return conversation/message ids (status {status})."
        )

    for _ in range(max_polls):
        time.sleep(poll_seconds)
        polled = session.call_tool(
            f"poll_response_{space}",
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
            },
        )
        status = polled.get("status")
        if status not in _IN_PROGRESS:
            return polled.get("content", {}), status

    raise TimeoutError(
        f"Genie query did not finish within "
        f"{max_polls * poll_seconds}s (last status {status})."
    )


# ---------------------------------------------------------------- parsing


def _cell(value):
    """Unwrap one Databricks statement-response cell into a plain value."""
    if not isinstance(value, dict):
        return value
    if "string_value" in value:
        return value["string_value"]
    # Statement responses only use string_value here, but be tolerant.
    for key in ("long_value", "double_value", "boolean_value"):
        if key in value:
            return value[key]
    return None


def _rows_from_attachment(attachment):
    """Yield dict rows (column name -> value) from one query attachment."""
    sr = attachment.get("statement_response", {})
    columns = [
        c.get("name") for c in sr.get("manifest", {}).get("schema", {}).get(
            "columns", []
        )
    ]
    for row in sr.get("result", {}).get("data_array", []):
        values = row.get("values", []) if isinstance(row, dict) else row
        yield {
            col: _cell(values[i]) if i < len(values) else None
            for i, col in enumerate(columns)
        }


def _first_present(row, candidates):
    """Return the value of the first candidate column present and non-empty."""
    lower = {k.lower(): v for k, v in row.items()}
    for name in candidates:
        if name in lower and lower[name]:
            return lower[name]
    return None


def _element_label(row):
    """Build a human label for an element from whatever columns exist."""
    lower = {k.lower(): v for k, v in row.items()}
    parts = []
    for key in ("category_name", "family_name", "type_name"):
        if lower.get(key):
            parts.append(str(lower[key]))
    label = " \u2013 ".join(parts)  # en-dash between parts
    asset = lower.get("asset_id")
    if asset:
        label = f"{label} ({asset})" if label else str(asset)
    return label or None


def shape_response(content):
    """Turn a Genie ``content`` payload into {reply, elements, rooms}."""
    text_attachments = content.get("textAttachments") or []
    reply = "\n\n".join(t for t in text_attachments if t).strip()

    elements = {}
    space_ids = []
    # Preserve the column order Genie returned so CSV columns stay sensible.
    field_order = []
    for attachment in content.get("queryAttachments") or []:
        for row in _rows_from_attachment(attachment):
            for col in row:
                if col not in field_order:
                    field_order.append(col)
            ext = _first_present(row, _EXTERNAL_ID_COLS)
            if ext and ext not in elements:
                elements[ext] = {
                    "external_id": ext,
                    "label": _element_label(row),
                    # Carry every column Genie returned for this row, so the
                    # viewer can show / export exactly what was asked for.
                    "fields": {k: v for k, v in row.items()},
                }
            space = _first_present(row, _SPACE_ID_COLS)
            if space and space not in space_ids:
                space_ids.append(space)

    rooms = _fetch_rooms(space_ids)

    return {
        "reply": reply or "(no answer)",
        "field_order": field_order,
        "elements": list(elements.values()),
        "rooms": rooms,
    }


def _fetch_rooms(space_ids):
    """Load room geometry for the given space_external_ids from mech_rooms."""
    if not space_ids:
        return []

    placeholders = ", ".join(f":id{i}" for i in range(len(space_ids)))
    params = {f"id{i}": sid for i, sid in enumerate(space_ids)}
    sql = f"""
        SELECT
            unique_id              AS space_external_id,
            room_name,
            room_number,
            level_name,
            wkt,
            height,
            location_point.z       AS base_z,
            area,
            volume
        FROM {ROOMS_TABLE}
        WHERE unique_id IN ({placeholders})
    """
    try:
        rows = query_rows(sql, params)
    except Exception:
        # Room geometry is a nice-to-have; a failure here must not break chat.
        return []

    rooms = []
    for r in rows:
        label = r.get("room_name") or r.get("room_number") or r.get(
            "space_external_id"
        )
        rooms.append(
            {
                "space_external_id": r.get("space_external_id"),
                "label": label,
                "level_name": r.get("level_name"),
                "wkt": r.get("wkt"),
                "base_z": r.get("base_z"),
                "height": r.get("height"),
                "area": r.get("area"),
                "volume": r.get("volume"),
            }
        )
    return rooms


def answer(query):
    """End-to-end: ask Genie, wait, and return the viewer contract."""
    content, status = ask_genie(query)
    shaped = shape_response(content)
    if status not in ("COMPLETED",) and not shaped["reply"]:
        shaped["reply"] = f"Genie returned status {status} with no answer."
    return shaped
