"""Read-only probe: drive the LIVE browser viewer through the MCP tools.

This does NOT touch the live viewer project. It opens the bim_viewer_mcp server
in-memory (the same way the Phoenix eval does), then calls its real tools with
VIEWER_BASE_URL pointed at the running app.py backend. The tools POST validated
commands to that backend, which broadcasts them to every connected browser
viewer over its WebSocket -- so the open viewer tab should light up.

It also times each step so we can see how much of the ~16s we observed on the
Genie path is the Databricks query (shared by both paths) versus everything
else (Genie's model steps, which the MCP path skips).

Prereqs:
  - The live backend is running at VIEWER_BASE_URL (default http://127.0.0.1:8000)
  - A browser viewer tab is open at that URL with the model loaded

Run from the hackathon repo root (uses this repo's .venv, which has fastmcp):
    $env:VIEWER_BASE_URL = "http://127.0.0.1:8000"
    $env:PYTHONUTF8 = "1"
    .\.venv\Scripts\python.exe probe_live_viewer.py
"""

from __future__ import annotations

import asyncio
import os
import time

# Point the MCP tools at the live backend BEFORE importing the server/config,
# since config reads the environment at import time.
os.environ.setdefault("VIEWER_BASE_URL", "http://127.0.0.1:8000")

from fastmcp import Client  # noqa: E402


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in (result.content or []))


async def _timed(client: Client, label: str, tool: str, args: dict):
    print(f"\n>>> {label}")
    print(f"    tool={tool} args={args}")
    t0 = time.perf_counter()
    result = await client.call_tool(tool, args)
    dt = time.perf_counter() - t0
    out = _text(result)
    print(f"    took {dt:6.2f}s")
    print(f"    -> {out[:400]}")
    return out, dt


async def main() -> None:
    base = os.environ["VIEWER_BASE_URL"]
    print(f"VIEWER_BASE_URL = {base}")
    print("Make sure a viewer tab is open at that URL with the model loaded.")

    from bim_viewer_mcp.app import mcp

    async with Client(mcp) as client:
        specs = await client.list_tools()
        print(f"\nMCP tools available: {[s.name for s in specs]}")

        # Step 1: resolve the cooling tower to its externalId (Databricks query).
        out, find_dt = await _timed(
            client,
            "find the cooling tower (data step -- hits Databricks)",
            "find_by_category",
            {"category": "cooling_tower"},
        )

        # Pull the external_id out of the returned JSON text.
        import json

        try:
            data = json.loads(out)
            ids = [
                e["external_id"]
                for e in data.get("elements", [])
                if e.get("external_id")
            ]
        except Exception:
            ids = []

        if not ids:
            print("\nNo external_ids returned -- cannot highlight. "
                  "Is the backend up and the warehouse reachable?")
            return

        print(f"\n    resolved {len(ids)} external_id(s): {ids}")

        # Step 2: highlight it in the live viewer (no Databricks -- pure broadcast).
        await _timed(
            client,
            "highlight it in the LIVE viewer (broadcast step -- no Databricks)",
            "highlight_elements",
            {"external_ids": ids},
        )

        print("\n>>> Look at the browser viewer -- the cooling tower should now "
              "be highlighted.")

        # Step 3: a second category to compare timing once the warehouse is warm.
        await _timed(
            client,
            "find + the air terminals on Level 2 (warm-warehouse timing)",
            "find_by_category",
            {"category": "air_terminals", "level": "Level 2"},
        )

    print("\nDone. Timings above: the 'data step' is shared with the Genie path; "
          "the 'broadcast step' is the part MCP does instead of Genie's model "
          "reasoning.")


if __name__ == "__main__":
    asyncio.run(main())
