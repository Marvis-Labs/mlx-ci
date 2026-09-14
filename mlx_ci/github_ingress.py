from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class GitHubIngressError(ValueError):
    pass


COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
)
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")


class GitHubClient(Protocol):
    def collaborator_permission(self, repository: str, username: str) -> str: ...

    def pull_request(self, repository: str, number: int) -> Mapping[str, Any]: ...

    def issue_comment(self, repository: str, comment_id: int) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class RepositoryRegistration:
    repository: str
    contract_sha: str | None = None

    def __post_init__(self):
        if REPOSITORY_PATTERN.fullmatch(self.repository) is None:
            raise GitHubIngressError("registered repository is invalid")
        if (
            self.contract_sha is not None
            and COMMIT_PATTERN.fullmatch(self.contract_sha) is None
        ):
            raise GitHubIngressError("registered contract_sha is invalid")


@dataclass(frozen=True)
class AuthorizedRun:
    request: dict[str, Any]
    base_sha: str
    head_sha: str
    head_repository: str
    contract_sha: str


class IngressOutcome(StrEnum):
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    DENIED = "denied"


@dataclass(frozen=True)
class IngressDecision:
    outcome: IngressOutcome
    reason: str
    run: AuthorizedRun | None = None


class GitHubIngress:
    def __init__(
        self,
        registrations: Sequence[RepositoryRegistration],
        client: GitHubClient,
    ):
        self.client = client
        self.registrations: dict[str, RepositoryRegistration] = {}
        for registration in registrations:
            if registration.repository in self.registrations:
                raise GitHubIngressError("repository registration is duplicated")
            self.registrations[registration.repository] = registration

    def issue_comment(
        self, event: Mapping[str, Any], *, delivery_id: str
    ) -> IngressDecision:
        if event.get("action") != "created":
            return IngressDecision(IngressOutcome.IGNORED, "unsupported_action")

        repository = _string(_path(event, "repository", "full_name"), "repository")
        registration = self.registrations.get(repository)
        if registration is None:
            return IngressDecision(IngressOutcome.IGNORED, "repository_not_registered")

        issue = _mapping(_path(event, "issue"), "issue")
        if not isinstance(issue.get("pull_request"), Mapping):
            return IngressDecision(IngressOutcome.IGNORED, "not_a_pull_request")

        comment = _mapping(_path(event, "comment"), "comment")
        if comment.get("body") != "/ci run":
            return IngressDecision(IngressOutcome.IGNORED, "unsupported_command")

        sender = _string(_path(event, "sender", "login"), "sender.login")
        permission = self.client.collaborator_permission(repository, sender)
        if permission not in {"admin", "maintain", "write"}:
            return IngressDecision(IngressOutcome.DENIED, "insufficient_permission")

        pull_request_number = _positive_int(issue.get("number"), "issue.number")
        resolved = self._pull_request(repository, pull_request_number)
        if resolved is None:
            return IngressDecision(IngressOutcome.IGNORED, "pull_request_not_open")
        pull_request, base_sha, head_sha, head_repository = resolved
        request = {
            "schema_version": 1,
            "kind": "run_request",
            "request_id": f"github:{delivery_id}",
            "repository": repository,
            "pull_request": pull_request_number,
            "comment_id": _positive_int(comment.get("id"), "comment.id"),
            "requester": sender,
            "requested_at": _string(comment.get("created_at"), "comment.created_at"),
        }
        request = _validate_request(request)

        return IngressDecision(
            IngressOutcome.ACCEPTED,
            "authorized",
            AuthorizedRun(
                request=request,
                base_sha=base_sha,
                head_sha=head_sha,
                head_repository=head_repository,
                contract_sha=registration.contract_sha or base_sha,
            ),
        )

    def repository_dispatch(self, event: Mapping[str, Any]) -> IngressDecision:
        action = event.get("action")
        if action == "ci-plan-request":
            return self._repository_plan_dispatch(event)
        if action != "ci-run-request":
            return IngressDecision(IngressOutcome.IGNORED, "unsupported_action")
        payload = _mapping(event.get("client_payload"), "client_payload")
        if (
            set(payload)
            != {
                "schema_version",
                "repository",
                "pull_request",
                "comment_id",
            }
            or payload.get("schema_version") != 1
        ):
            raise GitHubIngressError("repository dispatch payload is invalid")
        repository = _repository(payload.get("repository"), "repository")
        if repository not in self.registrations:
            return IngressDecision(IngressOutcome.IGNORED, "repository_not_registered")
        pull_request = _positive_int(payload.get("pull_request"), "pull_request")
        comment_id = _positive_int(payload.get("comment_id"), "comment_id")
        comment = _mapping(
            self.client.issue_comment(repository, comment_id), "issue_comment"
        )
        if _positive_int(comment.get("id"), "issue_comment.id") != comment_id:
            raise GitHubIngressError("issue comment identity does not match request")
        expected_issue_url = (
            f"https://api.github.com/repos/{repository}/issues/{pull_request}"
        )
        if (
            _string(comment.get("issue_url"), "issue_comment.issue_url")
            != expected_issue_url
        ):
            raise GitHubIngressError(
                "issue comment pull request does not match request"
            )
        sender = _string(_path(comment, "user", "login"), "issue_comment.user.login")
        issue_comment_event = {
            "action": "created",
            "repository": {"full_name": repository},
            "issue": {"number": pull_request, "pull_request": {}},
            "comment": {
                "id": comment_id,
                "body": comment.get("body"),
                "created_at": comment.get("created_at"),
            },
            "sender": {"login": sender},
        }
        return self.issue_comment(
            issue_comment_event, delivery_id=f"comment:{comment_id}"
        )

    def _repository_plan_dispatch(self, event: Mapping[str, Any]) -> IngressDecision:
        payload = _mapping(event.get("client_payload"), "client_payload")
        if (
            set(payload)
            != {"schema_version", "repository", "pull_request", "delivery_id"}
            or payload.get("schema_version") != 1
        ):
            raise GitHubIngressError("repository dispatch payload is invalid")
        repository = _repository(payload.get("repository"), "repository")
        registration = self.registrations.get(repository)
        if registration is None:
            return IngressDecision(IngressOutcome.IGNORED, "repository_not_registered")
        pull_request_number = _positive_int(payload.get("pull_request"), "pull_request")
        delivery_id = _positive_int(payload.get("delivery_id"), "delivery_id")
        resolved = self._pull_request(repository, pull_request_number)
        if resolved is None:
            return IngressDecision(IngressOutcome.IGNORED, "pull_request_not_open")
        pull_request, base_sha, head_sha, head_repository = resolved
        request = _validate_request(
            {
                "schema_version": 1,
                "kind": "run_request",
                "request_id": f"github:plan:{delivery_id}",
                "repository": repository,
                "pull_request": pull_request_number,
                "comment_id": delivery_id,
                "requester": "github-actions",
                "requested_at": _string(
                    pull_request.get("updated_at"), "pull_request.updated_at"
                ),
            }
        )
        return IngressDecision(
            IngressOutcome.ACCEPTED,
            "authorized",
            AuthorizedRun(
                request=request,
                base_sha=base_sha,
                head_sha=head_sha,
                head_repository=head_repository,
                contract_sha=registration.contract_sha or base_sha,
            ),
        )

    def _pull_request(
        self, repository: str, number: int
    ) -> tuple[Mapping[str, Any], str, str, str] | None:
        pull_request = _mapping(
            self.client.pull_request(repository, number), "pull_request"
        )
        if pull_request.get("state") != "open":
            return None
        if _positive_int(pull_request.get("number"), "pull_request.number") != number:
            raise GitHubIngressError("pull request number does not match request")
        if _repository_path(pull_request, "base", "repo", "full_name") != repository:
            raise GitHubIngressError(
                "pull request base repository does not match request"
            )
        return (
            pull_request,
            _commit_path(pull_request, "base", "sha"),
            _commit_path(pull_request, "head", "sha"),
            _repository_path(pull_request, "head", "repo", "full_name"),
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GitHubIngressError(f"{field} must be an object")
    return value


def _path(value: Mapping[str, Any], *parts: str) -> Any:
    current: Any = value
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            raise GitHubIngressError(f"missing GitHub event field: {'.'.join(parts)}")
        current = current[part]
    return current


def _commit_path(value: Mapping[str, Any], *parts: str) -> str:
    field = ".".join(("pull_request", *parts))
    commit = _string(_path(value, *parts), field)
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise GitHubIngressError(f"{field} must be an immutable commit SHA")
    return commit


def _repository_path(value: Mapping[str, Any], *parts: str) -> str:
    field = ".".join(parts)
    return _repository(_path(value, *parts), field)


def _repository(value: Any, field: str) -> str:
    repository = _string(value, field)
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise GitHubIngressError(f"{field} must be a repository name")
    return repository


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise GitHubIngressError(f"{field} must be a non-empty string")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GitHubIngressError(f"{field} must be a positive integer")
    return value


def _validate_request(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "request_id",
        "repository",
        "pull_request",
        "comment_id",
        "requester",
        "requested_at",
    }
    if set(value) != fields:
        raise GitHubIngressError("GitHub request fields are invalid")
    if value.get("schema_version") != 1 or value.get("kind") != "run_request":
        raise GitHubIngressError("GitHub request schema is invalid")
    for field in ("request_id", "requester"):
        item = value.get(field)
        if not isinstance(item, str) or IDENTIFIER_PATTERN.fullmatch(item) is None:
            raise GitHubIngressError(f"GitHub run request {field} is invalid")
    _repository(value.get("repository"), "request.repository")
    _positive_int(value.get("pull_request"), "request.pull_request")
    _positive_int(value.get("comment_id"), "request.comment_id")
    timestamp = value.get("requested_at")
    if not isinstance(timestamp, str) or TIMESTAMP_PATTERN.fullmatch(timestamp) is None:
        raise GitHubIngressError("GitHub request requested_at is invalid")
    return dict(value)
