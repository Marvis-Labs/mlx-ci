from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from mlx_ci.github_api import GitHubAPI
from mlx_ci.github_ingress import (
    GitHubIngress,
    GitHubIngressError,
    IngressOutcome,
    RepositoryRegistration,
)

MAX_EVENT_BYTES = 256_000


def authorize_dispatch(
    event: Mapping[str, object], *, token: str, repositories: Sequence[str]
) -> dict[str, object]:
    registrations = [RepositoryRegistration(value) for value in repositories]
    decision = GitHubIngress(registrations, GitHubAPI(token)).repository_dispatch(event)
    if decision.outcome != IngressOutcome.ACCEPTED or decision.run is None:
        raise GitHubIngressError(f"CI request was not accepted: {decision.reason}")
    run = decision.run
    return {
        "request": run.request,
        "comment_id": run.request["comment_id"],
        "repository": run.request["repository"],
        "pull_request": run.request["pull_request"],
        "base_sha": run.base_sha,
        "head_sha": run.head_sha,
        "head_repository": run.head_repository,
        "contract_sha": run.contract_sha,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--owner")
    parser.add_argument("--repository", action="append", default=[])
    parser.add_argument("--repositories")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if args.event.is_symlink() or args.event.stat().st_size > MAX_EVENT_BYTES:
        raise GitHubIngressError("GitHub event file is invalid")
    event = json.loads(args.event.read_text())
    if not isinstance(event, Mapping):
        raise GitHubIngressError("GitHub event must be an object")
    token = os.environ.get("GITHUB_APP_TOKEN", "")
    configured = [
        *args.repository,
        *(
            line.strip()
            for line in (args.repositories or "").splitlines()
            if line.strip()
        ),
    ]
    if not configured:
        raise GitHubIngressError("at least one repository must be registered")
    repositories = [
        value if "/" in value else f"{args.owner}/{value}"
        for value in configured
        if args.owner or "/" in value
    ]
    if len(repositories) != len(configured):
        raise GitHubIngressError("repository names require an owner")
    result = authorize_dispatch(event, token=token, repositories=repositories)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, args.output)
    if args.github_output is not None:
        with args.github_output.open("a") as stream:
            for field in (
                "repository",
                "pull_request",
                "comment_id",
                "base_sha",
                "head_sha",
                "head_repository",
                "contract_sha",
            ):
                stream.write(f"{field}={result[field]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
