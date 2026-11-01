# Evaluation

The server is evaluated with an [Arize Phoenix](https://docs.arize.com/phoenix)
harness that measures how well an agent uses the tools — not how many tools
exist, but whether an LLM can pick the right tool on the right elements and
decline out-of-scope requests.

Everything lives in [`phoenix/`](phoenix/):

```
phoenix/
├── agent.py            # BimViewerAgent — launches the MCP server over stdio
├── run_experiment.py   # CLI: run agent + judges against a dataset
├── create_dataset.py   # CLI: upload a CSV dataset to Phoenix
├── datasets.yaml       # Dataset registry
├── judges/             # LLM-as-judge evaluators (correctness, scope)
├── prompts/            # Versioned agent system prompts (v1–v3)
└── datasets/
    └── bim-viewer-eval-0/   # 16 gold questions (evaluation.xml → examples/*.csv)
```

- **Full methodology and results:**
  [`../docs/project/EVALUATION_WRITEUP.md`](../docs/project/EVALUATION_WRITEUP.md)
- **Step-by-step run guide:**
  [`../docs/project/HOW_TO_RUN_THE_EVAL.md`](../docs/project/HOW_TO_RUN_THE_EVAL.md)

Ten gold questions span category resolves, cost rankings, camera moves, resets,
and out-of-scope refusals. Two LLM judges score **correctness** (right tool,
right targets) and **scope** (refuse out-of-authority requests). Re-running with
the tools hidden isolates the server's contribution: **+0.30 correctness**
(0.70 → 1.00), with scope at 1.0 in both configurations.
