from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from runners import github
from runners.contract import ContractError, read_json, validate_attempt
from runners.engines import load_engines


def _time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError("GitHub run timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ContractError("GitHub run timestamp is invalid")
    return parsed


def _artifact_attempt(content: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            matches = [
                item for item in archive.infolist() if item.filename == "attempt.json"
            ]
            if len(matches) != 1 or matches[0].file_size > 65_536:
                raise ContractError("attempt artifact is invalid")
            value = json.loads(archive.read(matches[0]))
    except (KeyError, OSError, UnicodeError, ValueError, zipfile.BadZipFile) as error:
        raise ContractError("attempt artifact is invalid") from error
    if not isinstance(value, dict):
        raise ContractError("attempt artifact is invalid")
    return value


def same_active_attempt(
    attempt: dict[str, Any], run: dict[str, Any], previous: dict[str, Any]
) -> bool:
    identity = ("repository", "pull_request", "base_sha", "head_sha")
    if run.get("id") == attempt["run_id"] or any(
        previous.get(field) != attempt[field] for field in identity
    ):
        return False
    return run.get("status") != "completed" or (
        run.get("conclusion") == "success"
        and _time(run.get("updated_at")) >= _time(attempt["requested_at"])
    )


def has_active_attempt(
    attempt: dict[str, Any],
    orchestrator: str,
    token: str,
    repositories: dict[str, str],
) -> bool:
    value = github.get(
        f"repos/{orchestrator}/actions/runs?event=repository_dispatch&per_page=30",
        token,
    )
    if not isinstance(value, dict) or not isinstance(value.get("workflow_runs"), list):
        raise ContractError("GitHub runs response is invalid")
    for run in value["workflow_runs"]:
        if (
            not isinstance(run, dict)
            or type(run.get("id")) is not int
            or run["id"] >= attempt["run_id"]
            or run.get("event") != "repository_dispatch"
        ):
            continue
        if run.get("status") == "completed" and _time(run.get("updated_at")) < _time(
            attempt["requested_at"]
        ):
            continue
        artifacts = github.get(
            f"repos/{orchestrator}/actions/runs/{run['id']}/artifacts?per_page=100",
            token,
        )
        if not isinstance(artifacts, dict) or not isinstance(
            artifacts.get("artifacts"), list
        ):
            raise ContractError("GitHub artifacts response is invalid")
        name = f"ci-attempt-{run['id']}-{run.get('run_attempt')}"
        matches = [item for item in artifacts["artifacts"] if item.get("name") == name]
        if len(matches) != 1 or not isinstance(
            matches[0].get("archive_download_url"), str
        ):
            continue
        previous = _artifact_attempt(
            github.read(matches[0]["archive_download_url"], token, 2_000_000)
        )
        try:
            validate_attempt(previous, repositories)
        except ContractError:
            continue
        if same_active_attempt(attempt, run, previous):
            return True
    return False


def prepare(
    event: dict,
    engines: dict[str, dict[str, Any]],
    token: str,
    run_id: int,
    run_attempt: int,
) -> dict:
    if event.get("action") != "ci-run-request":
        raise ContractError("unsupported event")
    payload = event.get("client_payload")
    if not isinstance(payload, dict):
        raise ContractError("request payload is invalid")
    attempt = github.resolve_request(
        payload,
        engines,
        lambda path: github.get(path, token),
        lambda path: github.files(path, token),
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
    engines = load_engines(arguments.engines)
    attempt = prepare(read_json(arguments.event), engines, token, run_id, run_attempt)
    actions_token = os.environ.get("CI_ACTIONS_TOKEN", "")
    orchestrator = os.environ.get("CI_ORCHESTRATOR_REPOSITORY", "")
    if len(actions_token) < 20 or "\n" in actions_token or not orchestrator:
        raise ContractError("workflow token is unavailable")
    repositories = {name: engine["repository"] for name, engine in engines.items()}
    duplicate = has_active_attempt(attempt, orchestrator, actions_token, repositories)
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
            stream.write(f"coalesced={'true' if duplicate else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
