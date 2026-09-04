from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from mlx_ci.contracts import (
    DIGEST_PATTERN,
    IDENTIFIER_PATTERN,
    ContractError,
    validate_assignment,
    validate_result,
    validate_runner,
    validate_runner_response,
)
from mlx_ci.control_plane import ControlPlane, SubmissionReceipt
from mlx_ci.scheduler import Assignment, Scheduler

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")


class AuthenticationError(RuntimeError):
    pass


class ManifestSigner(Protocol):
    def sign(self, manifest: dict[str, Any]) -> dict[str, Any]: ...


class RunnerAuthenticator:
    def __init__(self, token_digests: Mapping[str, str]):
        if not token_digests:
            raise ValueError("at least one runner credential is required")
        self._token_digests = {}
        for runner_id, digest in token_digests.items():
            if IDENTIFIER_PATTERN.fullmatch(runner_id) is None:
                raise ValueError("runner credential id is invalid")
            if DIGEST_PATTERN.fullmatch(digest) is None:
                raise ValueError("runner credential digest is invalid")
            self._token_digests[runner_id] = digest

    @classmethod
    def from_file(cls, value: str | Path) -> RunnerAuthenticator:
        path = Path(value)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise ValueError("runner credential file is unavailable") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("runner credential file must be a regular file")
            if metadata.st_mode & 0o077:
                raise ValueError(
                    "runner credential file permissions must be owner-only"
                )
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise ValueError("runner credential file must be service-owned")
            if metadata.st_size > 65_536:
                raise ValueError("runner credential file is too large")
            with os.fdopen(descriptor) as stream:
                descriptor = -1
                record = json.load(stream)
        except (OSError, ValueError) as error:
            if isinstance(error, ValueError) and str(error).startswith(
                "runner credential file"
            ):
                raise
            raise ValueError("runner credential file is invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(record, Mapping) or set(record) != {
            "schema_version",
            "kind",
            "token_digests",
        }:
            raise ValueError("runner credential file is invalid")
        if record["schema_version"] != 1 or record["kind"] != "runner_credentials":
            raise ValueError("runner credential file is invalid")
        token_digests = record["token_digests"]
        if not isinstance(token_digests, Mapping):
            raise ValueError("runner credential file is invalid")
        return cls(token_digests)

    @staticmethod
    def digest(token: str) -> str:
        if TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError("runner token is invalid")
        return "sha256:" + hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self, runner_id: str, token: str) -> None:
        expected = self._token_digests.get(runner_id, "sha256:" + "0" * 64)
        if (
            not authenticate_token(expected, token)
            or runner_id not in self._token_digests
        ):
            raise AuthenticationError("runner authentication failed")


class ControlService:
    def __init__(
        self,
        submissions: ControlPlane,
        scheduler: Scheduler,
        signer: ManifestSigner,
        authenticator: RunnerAuthenticator,
        *,
        dispatch_limit: int = 512,
    ):
        if scheduler.store is not submissions.store:
            raise ValueError("scheduler and submissions must use the same store")
        if not isinstance(dispatch_limit, int) or not 1 <= dispatch_limit <= 512:
            raise ValueError("dispatch_limit must be between 1 and 512")
        self.submissions = submissions
        self.store = submissions.store
        self.scheduler = scheduler
        self.signer = signer
        self.authenticator = authenticator
        self.dispatch_limit = dispatch_limit

    def submit(
        self,
        request: dict[str, Any],
        plan: dict[str, Any],
        *,
        attempt_id: str,
    ) -> SubmissionReceipt:
        return self.submissions.submit(request, plan, attempt_id=attempt_id)

    def poll(
        self, capability: Mapping[str, Any], *, token: str, at: datetime
    ) -> dict[str, Any] | None:
        at = _utc(at)
        capability = _server_timestamped_capability(capability, at=at)
        runner_id = capability["runner_id"]
        self.authenticator.authenticate(runner_id, token)
        self.store.record_runner(capability)
        assignment = self.store.get_active_assignment(runner_id, now=_timestamp(at))
        if assignment is None:
            self._dispatch_available(at=at)
            assignment = self.store.get_active_assignment(runner_id, now=_timestamp(at))
        if assignment is None:
            return None
        return self._bundle(assignment)

    def renew(
        self,
        lease_id: str,
        *,
        runner_id: str,
        generation: str,
        token: str,
        at: datetime,
    ) -> dict[str, Any]:
        self.authenticator.authenticate(runner_id, token)
        return self.scheduler.renew(
            lease_id,
            runner_id=runner_id,
            generation=generation,
            at=_utc(at),
        )

    def respond(
        self, response: Mapping[str, Any], *, token: str, at: datetime
    ) -> dict[str, Any]:
        response = validate_runner_response(response)
        self.authenticator.authenticate(response["runner_id"], token)
        return self.scheduler.respond(dict(response), at=_utc(at))

    def complete(
        self,
        result: Mapping[str, Any],
        *,
        generation: str,
        token: str,
        at: datetime,
    ) -> dict[str, Any]:
        result = validate_result(result)
        self.authenticator.authenticate(result["runner_id"], token)
        return self.scheduler.complete(dict(result), generation=generation, at=_utc(at))

    def _dispatch_available(self, *, at: datetime) -> None:
        for _ in range(self.dispatch_limit):
            if self.scheduler.dispatch(at=at) is None:
                return

    def _bundle(self, assignment: Mapping[str, Any] | Assignment) -> dict[str, Any]:
        if isinstance(assignment, Assignment):
            manifest = assignment.manifest
            lease = assignment.lease
        else:
            manifest = assignment["manifest"]
            lease = assignment["lease"]
        return validate_assignment(
            {
                "schema_version": 1,
                "kind": "runner_assignment",
                "lease": lease,
                "envelope": self.signer.sign(manifest),
            }
        )


def _server_timestamped_capability(
    value: Mapping[str, Any], *, at: datetime
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("runner poll capability must be an object")
    expected = {
        "schema_version",
        "kind",
        "runner_id",
        "labels",
        "memory_gib",
        "available_disk_gib",
        "status",
    }
    if set(value) != expected:
        raise ContractError("runner poll capability fields are invalid")
    capability = dict(value)
    capability["heartbeat_at"] = _timestamp(at)
    capability = validate_runner(capability)
    if capability["status"] != "online":
        raise ContractError("polling runner must report online status")
    return capability


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ContractError("control-plane timestamps must be timezone-aware UTC")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def authenticate_token(expected_digest: str, token: str) -> bool:
    if DIGEST_PATTERN.fullmatch(expected_digest) is None:
        raise ValueError("token digest is invalid")
    try:
        supplied = RunnerAuthenticator.digest(token)
    except ValueError:
        supplied = "sha256:" + "f" * 64
    return hmac.compare_digest(expected_digest, supplied)
