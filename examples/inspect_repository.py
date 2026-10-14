"""Run after installing the package: python examples/inspect_repository.py owner repo."""
import argparse
import json
import sys

from github_analysis_agent import GitHubClient, list_directory, read_file
from github_analysis_agent.errors import GitHubError


def main():
    parser = argparse.ArgumentParser(description="List a GitHub directory and read one file")
    parser.add_argument("owner")
    parser.add_argument("repo")
    parser.add_argument("--path", default="")
    parser.add_argument("--file", default="README.md", help="Repository-relative file path")
    parser.add_argument("--ref", default=None)
    args = parser.parse_args()
    try:
        with GitHubClient() as client:
            entries = list_directory(client, args.owner, args.repo, args.path, args.ref)
            print(json.dumps(entries, ensure_ascii=False, indent=2))
            if not any(e["path"] == args.file and e["type"] == "file" for e in entries):
                print("Choose --file from the regular files listed above", file=sys.stderr)
                return 1
            result = read_file(client, args.owner, args.repo, args.file, args.ref)
            print(result["text"])
    except GitHubError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if exc.retry_after or exc.rate_limit_reset:
            print(f"retry_after={exc.retry_after}, reset={exc.rate_limit_reset}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
