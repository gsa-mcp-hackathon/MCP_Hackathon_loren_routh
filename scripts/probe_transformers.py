"""Read-only probe #2: highlight the transformers in the LIVE viewer via MCP."""

from __future__ import annotations

import asyncio
import json
import os
import time

os.environ.setdefault("VIEWER_BASE_URL", "http://127.0.0.1:8000")

from fastmcp import Client


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in (result.content or []))


async def _timed(client, label, tool, args):
    print(f"\n>>> {label}")
    print(f"    tool={tool} args={args}")
    t0 = time.perf_counter()
    result = await client.call_tool(tool, args)
    dt = time.perf_counter() - t0
    out = _text(result)
    print(f"    took {dt:6.2f}s")
    print(f"    -> {out[:500]}")
    return out


async def main() -> None:
    from bim_viewer_mcp.app import mcp

    async with Client(mcp) as client:
        out = await _timed(
            client,
            "find the transformers (data step -- Databricks)",
            "find_by_category",
            {"category": "transformers"},
        )
        data = json.loads(out)
        ids = [e["external_id"] for e in data.get("elements", []) if e.get("external_id")]
        print(f"\n    resolved {len(ids)} transformer(s): {ids}")
        if not ids:
            return
        await _timed(
            client,
            "highlight them in the LIVE viewer (broadcast step)",
            "highlight_elements",
            {"external_ids": ids},
        )
        print("\n>>> Look at the browser viewer -- the transformers should be highlighted.")


if __name__ == "__main__":
    asyncio.run(main())
