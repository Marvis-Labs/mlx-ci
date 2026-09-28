from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from runners.contract import ContractError, REPOSITORY, SHA, validate_request


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        redirected = super().redirect_request(request, fp, code, msg, headers, new_url)
        if redirected and urllib.parse.urlparse(new_url).hostname != "api.github.com":
            redirected.remove_header("Authorization")
        return redirected


def read(path: str, token: str, maximum: int = 1_000_000) -> bytes:
    url = (
        path
        if path.startswith("https://api.github.com/")
        else f"https://api.github.com/{path}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "marvis-mlx-ci",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.build_opener(_SafeRedirect).open(
            request, timeout=20
        ) as response:
            content = response.read(maximum + 1)
    except (OSError, urllib.error.HTTPError) as error:
        raise ContractError("GitHub request failed") from error
    if len(content) > maximum:
        raise ContractError("GitHub response is too large")
    return content


def get(path: str, token: str) -> dict | list:
    try:
        value = json.loads(read(path, token))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ContractError("GitHub response is invalid") from error
    if not isinstance(value, (dict, list)):
        raise ContractError("GitHub response is invalid")
    return value


def files(path: str, token: str) -> list[dict]:
    changed = []
    for page in range(1, 31):
        value = get(f"{path}?per_page=100&page={page}", token)
        if not isinstance(value, list):
            raise ContractError("GitHub file response is invalid")
        changed.extend(value)
        if len(value) < 100:
            return changed
    raise ContractError("pull request changes too many files")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} is invalid")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    return value


def resolve_request(
    request: dict[str, Any],
    engines: dict[str, dict[str, Any]],
    get: Callable[[str], dict[str, Any]],
    get_files: Callable[[str], list[dict[str, Any]]],
) -> dict[str, Any]:
    repositories = {name: engine["repository"] for name, engine in engines.items()}
    validate_request(request, repositories)
    repository = request["repository"]
    number = request["pull_request"]
    comment_id = request["comment_id"]
    comment = _object(
        get(f"repos/{repository}/issues/comments/{comment_id}"), "comment"
    )
    user = _object(comment.get("user"), "comment user")
    requested_at = comment.get("created_at")
    try:
        requested_time = datetime.fromisoformat(
            str(requested_at).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise ContractError("comment timestamp is invalid") from error
    allowed = {name.lower() for name in engines[request["engine"]]["maintainers"]}
    if (
        comment.get("id") != comment_id
        or comment.get("body") != "/ci run"
        or comment.get("issue_url")
        != f"https://api.github.com/repos/{repository}/issues/{number}"
        or not isinstance(user.get("login"), str)
        or user["login"].lower() not in allowed
        or requested_time.tzinfo is None
    ):
        raise ContractError("comment is not an authorized CI request")

    pull = _object(get(f"repos/{repository}/pulls/{number}"), "pull request")
    base = _object(pull.get("base"), "base")
    head = _object(pull.get("head"), "head")
    base_repo = _object(base.get("repo"), "base repository")
    head_repo = _object(head.get("repo"), "head repository")
    main = _object(get(f"repos/{repository}/branches/main"), "main branch")
    main_commit = _object(main.get("commit"), "main commit")
    base_sha = _sha(main_commit.get("sha"), "main sha")
    pr_base_sha = _sha(base.get("sha"), "PR base sha")
    head_sha = _sha(head.get("sha"), "head sha")
    if (
        pull.get("state") != "open"
        or pull.get("number") != number
        or base.get("ref") != "main"
        or base_repo.get("full_name") != repository
        or pr_base_sha == head_sha
        or not isinstance(head_repo.get("full_name"), str)
        or REPOSITORY.fullmatch(head_repo["full_name"]) is None
        or base_sha == head_sha
    ):
        raise ContractError("pull request is not eligible for CI")
    changed_files = []
    for item in get_files(f"repos/{repository}/pulls/{number}/files"):
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ContractError("pull request file is invalid")
        changed_files.append(item["filename"])
        if item.get("status") == "renamed":
            previous = item.get("previous_filename")
            if not isinstance(previous, str):
                raise ContractError("renamed pull request file is invalid")
            changed_files.append(previous)
    return {
        "engine": request["engine"],
        "repository": repository,
        "pull_request": number,
        "comment_id": comment_id,
        "requested_at": requested_at,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "head_repository": head_repo["full_name"],
        "contract_sha": base_sha,
        "changed_files": sorted(set(changed_files)),
    }
