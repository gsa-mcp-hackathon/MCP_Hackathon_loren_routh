"""BimViewerAgent — a LangChain agent wired to the BIM Viewer MCP server.

The agent connects to the `bim_viewer_mcp` server with FastMCP's in-memory
client, loads its tools, and answers natural-language viewer requests by calling
those tools. It also owns Phoenix OpenTelemetry instrumentation so every tool
call is traced.

The MCP tools are loaded in-process via `fastmcp.Client(mcp)` rather than the
LangChain `MultiServerMCPClient`: the current `mcp` (v2.x, required by our
FastMCP server) is incompatible with every published `langchain-mcp-adapters`
(which still requires `mcp` v1.x). The in-memory client runs the exact same tool
code and is functionally identical for evaluating tool CHOICE, which is what the
eval scores.

What we evaluate here is TOOL USE, not pixels: the BIM viewer does not need to
be running. The agent's job is to pick the right tool with the right arguments
(or to refuse out-of-scope requests). Whether a command actually paints a
browser is a separate, already-verified concern.

Standalone smoke test:
    uv run python eval/phoenix/agent.py
"""

from __future__ import annotations

import asyncio
import os

from langchain.agents import create_agent
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from openinference.instrumentation.langchain import LangChainInstrumentor

from fastmcp import Client

import phoenix.otel

from eval.phoenix import _env
from eval.phoenix._toolname import assert_tool_names_valid

DEFAULT_PROJECT = "bim-viewer-eval"
DEFAULT_PHOENIX_ENDPOINT = "http://localhost:6006"

# Timeouts so one unresponsive gateway call can't stall an entire experiment.
# LLM_REQUEST_TIMEOUT caps a single model call; RUN_TIMEOUT caps one example's
# full resolve->act loop (several sequential LLM + tool round-trips). A call
# that exceeds the cap fails THAT example instead of hanging every other one.
LLM_REQUEST_TIMEOUT = 120.0
RUN_TIMEOUT = 240.0

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


async def _load_mcp_tools(client: Client) -> list[StructuredTool]:
    """Build LangChain tools from the in-memory FastMCP client.

    Each MCP tool becomes a StructuredTool whose coroutine forwards the call to
    the server and returns its text content. Tool errors are returned as text
    (not raised) so the agent can see and recover from them, mirroring the
    viewer chat's behavior.
    """
    tools: list[StructuredTool] = []
    specs = await client.list_tools()
    assert_tool_names_valid([s.name for s in specs])  # Claude rejects names outside [a-zA-Z0-9_-]
    for spec in specs:
        name = spec.name

        def make_coroutine(tool_name: str):
            async def _call(**kwargs) -> str:
                try:
                    result = await client.call_tool(tool_name, kwargs)
                except Exception as exc:  # let the model see the error and recover
                    return f"Tool error: {exc}"
                return "".join(
                    getattr(c, "text", "") for c in (result.content or [])
                )

            return _call

        tools.append(
            StructuredTool.from_function(
                coroutine=make_coroutine(name),
                name=name,
                description=spec.description or "",
                args_schema=spec.input_schema,
            )
        )
    return tools


class BimViewerAgent:
    """Owns Phoenix instrumentation and builds a LangChain agent per run.

    The MCP client and agent are created inside run() (not cached on the
    instance) because Phoenix executes each example on its own event loop and
    the FastMCP in-memory client is bound to the loop it was opened on.
    """

    def __init__(
        self,
        system_prompt_version: str = "v1",
        project_name: str = DEFAULT_PROJECT,
        phoenix_endpoint: str = DEFAULT_PHOENIX_ENDPOINT,
        with_tools: bool = True,
    ) -> None:
        _env.load_eval_env()
        self.system_prompt_version = system_prompt_version
        self.project_name = project_name
        self.phoenix_endpoint = phoenix_endpoint
        self.with_tools = with_tools
        self._instrumented = False

    def _load_system_prompt(self) -> str:
        path = os.path.join(
            PROMPTS_DIR, f"system_prompt_{self.system_prompt_version}.txt"
        )
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _instrument(self) -> None:
        if self._instrumented:
            return
        tracer_provider = phoenix.otel.register(
            project_name=self.project_name,
            endpoint=self.phoenix_endpoint.rstrip("/") + "/v1/traces",
        )
        LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
        self._instrumented = True

    async def initialize(self) -> None:
        """Instrument Phoenix once. Tools/agent are built per-run (see run()).

        We do NOT open the MCP client here. Phoenix executes each example in its
        own event loop via asyncio.run, and a FastMCP in-memory client is bound
        to the loop it was entered on — so the client (and the agent that holds
        it) must be created and torn down inside each run() call's loop.
        """
        self._instrument()

    def _build_model(self) -> ChatOpenAI:
        return ChatOpenAI(
            model=_env.agent_model(),
            api_key=_env.require("USAI_API_KEY"),
            base_url=_env.usai_api_base(),
            temperature=0,
            timeout=LLM_REQUEST_TIMEOUT,
            max_retries=2,
        )

    async def run(self, query: str) -> str:
        """Answer one natural-language request; return the final text answer.

        Self-contained: opens the in-memory MCP client, loads tools, builds the
        agent, runs, and tears the client down — all on the current event loop.
        This matches Phoenix's one-loop-per-example execution model.

        Wrapped in an overall timeout so a stuck gateway call fails this one
        example instead of stalling the whole experiment.
        """
        return await asyncio.wait_for(self._run(query), timeout=RUN_TIMEOUT)

    async def _run(self, query: str) -> str:
        if not self._instrumented:
            self._instrument()

        if not self.with_tools:
            agent = create_agent(
                model=self._build_model(),
                tools=[],
                system_prompt=self._load_system_prompt(),
            )
            return self._final_text(
                await agent.ainvoke(
                    {"messages": [{"role": "user", "content": query}]}
                )
            )

        from bim_viewer_mcp.app import mcp

        async with Client(mcp) as client:
            tools = await _load_mcp_tools(client)
            agent = create_agent(
                model=self._build_model(),
                tools=tools,
                system_prompt=self._load_system_prompt(),
            )
            return self._final_text(
                await agent.ainvoke(
                    {"messages": [{"role": "user", "content": query}]}
                )
            )

    @staticmethod
    def _final_text(result) -> str:
        messages = result.get("messages", [])
        if not messages:
            return ""
        final = messages[-1]
        content = getattr(final, "content", final)
        if isinstance(content, list):
            parts = [
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            ]
            return "".join(parts)
        return str(content)

    def create_experiment_task(self):
        """Return a SYNC task(example) closure for Phoenix experiments.

        Phoenix experiment tasks must be synchronous, so we bridge to the async
        agent with a single, persistent event loop shared across every example.

        We deliberately do NOT use asyncio.run per example: that creates and
        closes a fresh loop each time, and the FastMCP in-memory client's
        network teardown can fire AFTER its loop is already closed, raising
        "RuntimeError: Event loop is closed". Phoenix then retries the example
        into the same broken pattern, which can stall a run for hours. One loop
        reused for the whole experiment avoids that race entirely. The agent
        still opens and closes its MCP client inside each run() call, so every
        example gets a clean client — just on a loop that stays alive.
        """
        loop = asyncio.new_event_loop()

        def task(example) -> str:
            query = example.input["query"]
            return loop.run_until_complete(self.run(query))

        return task


async def _main() -> None:
    agent = BimViewerAgent()
    await agent.initialize()
    smoke = "Highlight the air terminals on Level 2."
    print("Q:", smoke)
    print("A:", await agent.run(smoke))


if __name__ == "__main__":
    asyncio.run(_main())
