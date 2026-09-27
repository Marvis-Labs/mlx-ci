from __future__ import annotations

import argparse
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from runners.contract import ContractError, read_json, validate_attempt
from runners.engines import load_engines
from runners.github import resolve_request


def github_get(path: str, token: str) -> dict | list:
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "marvis-mlx-ci",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content = response.read(1_000_001)
    except (OSError, urllib.error.HTTPError) as error:
        raise ContractError("GitHub request failed") from error
    if len(content) > 1_000_000:
        raise ContractError("GitHub response is too large")
    try:
        value = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ContractError("GitHub response is invalid") from error
    if not isinstance(value, (dict, list)):
        raise ContractError("GitHub response is invalid")
    return value


def github_files(path: str, token: str) -> list[dict]:
    files = []
    for page in range(1, 31):
        value = github_get(f"{path}?per_page=100&page={page}", token)
        if not isinstance(value, list):
            raise ContractError("GitHub file response is invalid")
        files.extend(value)
        if len(value) < 100:
            return files
    raise ContractError("pull request changes too many files")


def prepare(
    event: dict, engines_directory: Path, token: str, run_id: int, run_attempt: int
) -> dict:
    if event.get("action") != "ci-run-request":
        raise ContractError("unsupported event")
    payload = event.get("client_payload")
    if not isinstance(payload, dict):
        raise ContractError("request payload is invalid")
    engines = load_engines(engines_directory)
    attempt = resolve_request(
        payload,
        engines,
        lambda path: github_get(path, token),
        lambda path: github_files(path, token),
    )
    attempt.update(schema_version=2, run_id=run_id, run_attempt=run_attempt)
    repositories = {name: engine["repository"] for name, engine in engines.items()}
    return validate_attempt(attempt, repositories)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True, type=Path)
    parser.add_argument("--engines", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--github-output", type=Path)
    arguments = parser.parse_args()
    token = os.environ.get("GH_TOKEN", "")
    if len(token) < 20 or "\n" in token:
        raise ContractError("GitHub App token is unavailable")
    try:
        run_id = int(os.environ["GITHUB_RUN_ID"])
        run_attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    except (KeyError, ValueError) as error:
        raise ContractError("workflow run identity is unavailable") from error
    attempt = prepare(
        read_json(arguments.event), arguments.engines, token, run_id, run_attempt
    )
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=arguments.output.parent, delete=False
    ) as stream:
        json.dump(attempt, stream, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, arguments.output)
    if arguments.github_output is not None:
        with arguments.github_output.open("a", encoding="utf-8") as stream:
            for field in ("engine", "repository", "contract_sha"):
                stream.write(f"{field}={attempt[field]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
