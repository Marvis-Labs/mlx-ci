from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SHA = re.compile(r"[0-9a-f]{40}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")
MAX_BYTES = 65_536
MAX_WORK = 16
STATUSES = frozenset({"passed", "failed", "skipped", "infrastructure_failure"})


class ContractError(ValueError):
    pass


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate field: {key}")
        value[key] = item
    return value


def read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ContractError("input must be a bounded regular file")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ContractError("invalid number")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ContractError("invalid JSON") from error
    if not isinstance(value, dict):
        raise ContractError("input must be an object")
    return value


def _fields(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractError(f"{label} fields are invalid")
    return value


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or NAME.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    return value


def _int(value: Any, label: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ContractError(f"{label} is invalid")
    return value


def validate_request(value: Any, repositories: dict[str, str]) -> dict[str, Any]:
    request = _fields(
        value,
        {"schema_version", "engine", "repository", "pull_request", "comment_id"},
        "request",
    )
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise ContractError("unsupported request version")
    engine = _name(request["engine"], "engine")
    repository = request["repository"]
    if not isinstance(repository, str) or REPOSITORY.fullmatch(repository) is None:
        raise ContractError("repository is invalid")
    if repositories.get(engine) != repository:
        raise ContractError("engine and repository do not match")
    _int(request["pull_request"], "pull_request", 1_000_000)
    _int(request["comment_id"], "comment_id", 10**18)
    return request


def validate_job(value: Any, repositories: dict[str, str]) -> dict[str, Any]:
    job = _fields(
        value,
        {
            "schema_version",
            "engine",
            "repository",
            "pull_request",
            "base_sha",
            "head_sha",
            "head_repository",
            "contract_sha",
            "id",
            "component",
            "subject",
            "required_memory_gib",
            "required_disk_gib",
            "manifest_digest",
        },
        "job",
    )
    if type(job["schema_version"]) is not int or job["schema_version"] != 1:
        raise ContractError("unsupported job version")
    if repositories.get(_name(job["engine"], "engine")) != job["repository"]:
        raise ContractError("engine and repository do not match")
    _int(job["pull_request"], "pull_request", 1_000_000)
    for field in ("base_sha", "head_sha", "contract_sha"):
        _sha(job[field], field)
    if job["contract_sha"] != job["base_sha"]:
        raise ContractError("CI contract must come from current main")
    if (
        not isinstance(job["head_repository"], str)
        or REPOSITORY.fullmatch(job["head_repository"]) is None
    ):
        raise ContractError("head repository is invalid")
    for field in ("id", "component", "subject"):
        _name(job[field], field)
    _int(job["required_memory_gib"], "required_memory_gib", 512)
    _int(job["required_disk_gib"], "required_disk_gib", 1_024)
    digest = _sha256(
        {key: item for key, item in job.items() if key != "manifest_digest"}
    )
    if job["manifest_digest"] != digest:
        raise ContractError("job digest does not match")
    return job


def seal_job(value: dict[str, Any], repositories: dict[str, str]) -> dict[str, Any]:
    job = {**value, "manifest_digest": _sha256(value)}
    return validate_job(job, repositories)


def validate_result(value: Any, job: dict[str, Any]) -> dict[str, Any]:
    result = _fields(
        value,
        {"schema_version", "job_id", "manifest_digest", "status", "checks"},
        "result",
    )
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise ContractError("unsupported result version")
    if (
        result["job_id"] != job["id"]
        or result["manifest_digest"] != job["manifest_digest"]
    ):
        raise ContractError("result does not match job")
    if not isinstance(result["status"], str) or result["status"] not in STATUSES:
        raise ContractError("result status is invalid")
    checks = result["checks"]
    if not isinstance(checks, list) or len(checks) > MAX_WORK:
        raise ContractError("result checks are invalid")
    for check in checks:
        _fields(check, {"name", "status"}, "check")
        _name(check["name"], "check name")
        if not isinstance(check["status"], str) or check["status"] not in STATUSES:
            raise ContractError("check status is invalid")
    if result["status"] == "passed" and (
        not checks or any(check["status"] != "passed" for check in checks)
    ):
        raise ContractError("a passing result needs passing checks")
    return result


def _sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
