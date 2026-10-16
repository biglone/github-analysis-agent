"""Analyze a repository using the bounded read-only AgentRuntime."""

import argparse
import logging
import sys

from github_analysis_agent import AgentRuntime
from github_analysis_agent.agent_runtime import AgentConfigurationError
from github_analysis_agent.errors import GitHubError
from github_analysis_agent.model_errors import ModelAdapterError


def main():
    parser = argparse.ArgumentParser(description="Analyze a GitHub repository structure with read-only tools")
    parser.add_argument("owner", help="GitHub repository owner")
    parser.add_argument("repo", help="GitHub repository name")
    parser.add_argument("--ref", help="Optional branch, tag, or commit")
    parser.add_argument("--task", default="分析这个仓库的项目结构，概括主要目录和文件的用途。",
                        help="Analysis request")
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--max-tool-calls", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=120.0, help="Maximum run time in seconds")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("github_analysis_agent.agent_runtime").setLevel(logging.INFO)
    try:
        with AgentRuntime(max_turns=args.max_turns, max_tool_calls=args.max_tool_calls,
                          timeout_seconds=args.timeout) as agent:
            result = agent.run(args.task, owner=args.owner, repo=args.repo, ref=args.ref)
    except (AgentConfigurationError, GitHubError, ModelAdapterError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if result.answer:
        print(result.answer)
    print(f"termination={result.termination_reason} model_calls={result.model_calls} "
          f"tool_calls={result.tool_calls} tokens={result.token_usage.total_tokens} "
          f"elapsed={result.elapsed_seconds:.2f}s")
    for name, stats in result.tool_stats.items():
        print(f"tool {name}: requested={stats.requested} succeeded={stats.succeeded} "
              f"failed={stats.failed} skipped={stats.skipped}")
    if result.error:
        print(f"run stopped: {result.error}", file=sys.stderr)
    return 0 if result.termination_reason == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
