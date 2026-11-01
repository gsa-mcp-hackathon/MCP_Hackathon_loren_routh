"""CLI: run a Phoenix experiment — agent + judges against a dataset.

Loads the Phoenix dataset, builds the BimViewerAgent with a chosen system-prompt
version, runs each query as a task, and scores the outputs with the correctness
and scope judges. Run from eval/phoenix/.

Examples:
    uv run python run_experiment.py --dataset-name bim-viewer-eval-0
    uv run python run_experiment.py --dataset-name bim-viewer-eval-0 \
        --system-prompt-version v2
    # No-tool baseline (agent has no MCP tools) to show tool lift:
    uv run python run_experiment.py --dataset-name bim-viewer-eval-0 --no-tools
"""

from __future__ import annotations

import argparse
import os
import sys

from eval.phoenix import _env
from eval.phoenix.agent import (
    DEFAULT_PHOENIX_ENDPOINT,
    DEFAULT_PROJECT,
    BimViewerAgent,
)
from eval.phoenix.judges import check_answer_scope, match_expected_response

PROMPT_VERSIONS = ["v1", "v2", "v3"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Phoenix experiment.")
    parser.add_argument("--dataset-name", required=True)
    parser.add_argument(
        "--system-prompt-version",
        default="v1",
        choices=PROMPT_VERSIONS,
    )
    parser.add_argument("--project-name", default=DEFAULT_PROJECT)
    parser.add_argument(
        "--phoenix-endpoint",
        default=os.environ.get("PHOENIX_ENDPOINT", DEFAULT_PHOENIX_ENDPOINT),
    )
    parser.add_argument("--experiment-description", default="")
    parser.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="Repeat each example N times to check consistency.",
    )
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help="No-tool baseline: run the agent WITHOUT the MCP tools to "
        "measure tool lift.",
    )
    args = parser.parse_args()

    _env.load_eval_env()
    from phoenix.client import Client

    client = Client(base_url=args.phoenix_endpoint)
    dataset = client.datasets.get_dataset(dataset=args.dataset_name)

    agent = BimViewerAgent(
        system_prompt_version=args.system_prompt_version,
        project_name=args.project_name,
        phoenix_endpoint=args.phoenix_endpoint,
        with_tools=not args.no_tools,
    )
    task = agent.create_experiment_task()

    tool_tag = "no-tools" if args.no_tools else "with-tools"
    description = (
        args.experiment_description
        or f"{args.system_prompt_version} / {tool_tag}"
    )

    client.experiments.run_experiment(
        dataset=dataset,
        task=task,
        evaluators=[match_expected_response, check_answer_scope],
        experiment_description=description,
        repetitions=args.repetitions,
    )


if __name__ == "__main__":
    sys.exit(main())
