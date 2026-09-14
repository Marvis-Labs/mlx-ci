from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from mlx_ci.github_ingress import GitHubIngressError


class GitHubAPI:
    def __init__(self, token: str, *, api_url: str = "https://api.github.com"):
        if not isinstance(token, str) or len(token) < 20 or "\n" in token:
            raise ValueError("GitHub token is invalid")
        self.token = token
        self.api_url = api_url.rstrip("/")

    def collaborator_permission(self, repository: str, username: str) -> str:
        value = self._get(
            f"repos/{_repository(repository)}/collaborators/{_segment(username)}/permission"
        )
        permission = value.get("permission")
        if not isinstance(permission, str):
            raise GitHubIngressError("GitHub permission response is invalid")
        return permission

    def pull_request(self, repository: str, number: int) -> Mapping[str, Any]:
        return self._get(f"repos/{_repository(repository)}/pulls/{number}")

    def issue_comment(self, repository: str, comment_id: int) -> Mapping[str, Any]:
        return self._get(
            f"repos/{_repository(repository)}/issues/comments/{comment_id}"
        )

    def _get(self, path: str) -> Mapping[str, Any]:
        request = urllib.request.Request(
            f"{self.api_url}/{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "marvis-mlx-ci",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.length is not None and response.length > 2_000_000:
                    raise GitHubIngressError("GitHub API response is too large")
                body = response.read(2_000_001)
        except (OSError, urllib.error.HTTPError) as error:
            raise GitHubIngressError("GitHub API request failed") from error
        if len(body) > 2_000_000:
            raise GitHubIngressError("GitHub API response is too large")
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, ValueError) as error:
            raise GitHubIngressError("GitHub API response is invalid") from error
        if not isinstance(value, Mapping):
            raise GitHubIngressError("GitHub API response is invalid")
        return value


def _repository(value: str) -> str:
    return urllib.parse.quote(value, safe="/")


def _segment(value: str) -> str:
    return urllib.parse.quote(value, safe="")
