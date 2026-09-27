from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

SHA = re.compile(r"[0-9a-f]{40}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")
MAX_BYTES = 65_536
MAX_WORK = 64
STATUSES = frozenset({"passed", "failed", "skipped", "infrastructure_failure"})
CATEGORIES = frozenset({"correctness", "performance", "infrastructure"})
CACHE_RESULTS = frozenset({"hit", "downloaded", "not_applicable"})
METRIC_VERDICTS = frozenset({"improved", "stable", "regressed", "advisory"})
JOB_FIELDS = {
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
    "phases",
    "work",
    "resources",
    "artifact",
    "estimated_peak_bytes",
    "required_memory_gib",
    "required_disk_gib",
    "manifest_digest",
}
UNSEALED_JOB_FIELDS = JOB_FIELDS - {
    "estimated_peak_bytes",
    "required_memory_gib",
    "required_disk_gib",
    "manifest_digest",
}


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


def _nonnegative_int(value: Any, label: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise ContractError(f"{label} is invalid")
    return value


def _text(value: Any, label: str, maximum: int = 160) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(character in value for character in "\0\r\n")
    ):
        raise ContractError(f"{label} is invalid")
    return value


def _number(value: Any, label: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or abs(value) > 10**15
    ):
        raise ContractError(f"{label} is invalid")
    return value


def _path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or "\0" in value
        or PurePosixPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in PurePosixPath(value).parts)
    ):
        raise ContractError("changed file is invalid")
    return value


def _phases(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 8
        or any(_name(phase, "phase") != phase for phase in value)
        or len(set(value)) != len(value)
    ):
        raise ContractError("job phases are invalid")
    return value


def _work(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ContractError("job work is invalid")
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError) as error:
        raise ContractError("job work is invalid") from error
    if len(encoded) > 32_768:
        raise ContractError("job work is too large")
    return value


def _resources(value: Any) -> dict[str, int]:
    resources = _fields(
        value,
        {
            "resident_bytes",
            "fixed_bytes",
            "bytes_per_unit",
            "units",
            "batch_size",
            "workspace_bytes",
        },
        "resources",
    )
    _int(resources["resident_bytes"], "resident_bytes", 4 * (1 << 40))
    _nonnegative_int(resources["fixed_bytes"], "fixed_bytes", 1 << 40)
    _nonnegative_int(resources["bytes_per_unit"], "bytes_per_unit", 1 << 30)
    _nonnegative_int(resources["units"], "units", 10_000_000)
    _int(resources["batch_size"], "batch_size", 1_024)
    _nonnegative_int(resources["workspace_bytes"], "workspace_bytes", 1 << 40)
    return resources


def _artifact(value: Any, resident_bytes: int) -> dict[str, Any] | None:
    if value is None:
        return None
    artifact = _fields(
        value,
        {"kind", "repository", "revision", "tensor_bytes"},
        "artifact",
    )
    if artifact["kind"] != "huggingface":
        raise ContractError("artifact kind is invalid")
    if (
        not isinstance(artifact["repository"], str)
        or REPOSITORY.fullmatch(artifact["repository"]) is None
    ):
        raise ContractError("artifact repository is invalid")
    _sha(artifact["revision"], "artifact revision")
    _int(artifact["tensor_bytes"], "artifact tensor_bytes", 4 * (1 << 40))
    if artifact["tensor_bytes"] != resident_bytes:
        raise ContractError("artifact and resident sizes differ")
    return artifact


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


def validate_attempt(value: Any, repositories: dict[str, str]) -> dict[str, Any]:
    attempt = _fields(
        value,
        {
            "schema_version",
            "engine",
            "repository",
            "pull_request",
            "comment_id",
            "base_sha",
            "head_sha",
            "head_repository",
            "contract_sha",
            "run_id",
            "run_attempt",
            "changed_files",
        },
        "attempt",
    )
    validate_request(
        {
            "schema_version": 1,
            "engine": attempt["engine"],
            "repository": attempt["repository"],
            "pull_request": attempt["pull_request"],
            "comment_id": attempt["comment_id"],
        },
        repositories,
    )
    if type(attempt["schema_version"]) is not int or attempt["schema_version"] != 2:
        raise ContractError("unsupported attempt version")
    for field in ("base_sha", "head_sha", "contract_sha"):
        _sha(attempt[field], field)
    if (
        attempt["contract_sha"] != attempt["base_sha"]
        or attempt["base_sha"] == attempt["head_sha"]
    ):
        raise ContractError("attempt revisions are invalid")
    head_repository = attempt["head_repository"]
    if (
        not isinstance(head_repository, str)
        or REPOSITORY.fullmatch(head_repository) is None
    ):
        raise ContractError("head repository is invalid")
    _int(attempt["run_id"], "run_id", 10**18)
    _int(attempt["run_attempt"], "run_attempt", 1_000)
    changed_files = attempt["changed_files"]
    if (
        not isinstance(changed_files, list)
        or not 1 <= len(changed_files) <= 3_000
        or len(set(changed_files)) != len(changed_files)
    ):
        raise ContractError("changed files are invalid")
    for changed_file in changed_files:
        _path(changed_file)
    return attempt


def validate_job(value: Any, repositories: dict[str, str]) -> dict[str, Any]:
    from runners.resources import ResourceError, calculate_requirements

    job = _fields(value, JOB_FIELDS, "job")
    if type(job["schema_version"]) is not int or job["schema_version"] != 2:
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
    _phases(job["phases"])
    _work(job["work"])
    resources = _resources(job["resources"])
    artifact = _artifact(job["artifact"], resources["resident_bytes"])
    try:
        peak, memory, disk = calculate_requirements(resources, artifact)
    except ResourceError as error:
        raise ContractError(str(error)) from error
    _int(job["estimated_peak_bytes"], "estimated_peak_bytes", 8 * (1 << 40))
    _int(job["required_memory_gib"], "required_memory_gib", 512)
    _int(job["required_disk_gib"], "required_disk_gib", 8_192)
    if (
        job["estimated_peak_bytes"] != peak
        or job["required_memory_gib"] != memory
        or job["required_disk_gib"] != disk
    ):
        raise ContractError("job resource requirements do not match")
    digest = _sha256(
        {key: item for key, item in job.items() if key != "manifest_digest"}
    )
    if job["manifest_digest"] != digest:
        raise ContractError("job digest does not match")
    return job


def seal_job(value: dict[str, Any], repositories: dict[str, str]) -> dict[str, Any]:
    from runners.resources import ResourceError, calculate_requirements

    plan = _fields(value, UNSEALED_JOB_FIELDS, "unsealed job")
    _phases(plan["phases"])
    _work(plan["work"])
    resources = _resources(plan["resources"])
    artifact = _artifact(plan["artifact"], resources["resident_bytes"])
    try:
        peak, memory, disk = calculate_requirements(resources, artifact)
    except ResourceError as error:
        raise ContractError(str(error)) from error
    derived = {
        **plan,
        "estimated_peak_bytes": peak,
        "required_memory_gib": memory,
        "required_disk_gib": disk,
    }
    job = {**derived, "manifest_digest": _sha256(derived)}
    return validate_job(job, repositories)


def seal_plan(
    attempt: dict[str, Any], plan: Any, repositories: dict[str, str]
) -> dict[str, Any]:
    validate_attempt(attempt, repositories)
    plan = _fields(plan, {"schema_version", "jobs", "blocked"}, "plan")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ContractError("unsupported plan version")
    templates = plan["jobs"]
    if not isinstance(templates, list) or len(templates) > MAX_WORK:
        raise ContractError("plan jobs are invalid")
    identity = {
        key: attempt[key]
        for key in (
            "engine",
            "repository",
            "pull_request",
            "base_sha",
            "head_sha",
            "head_repository",
            "contract_sha",
        )
    }
    jobs = []
    for template in templates:
        template = _fields(
            template,
            {
                "id",
                "component",
                "subject",
                "phases",
                "work",
                "resources",
                "artifact",
            },
            "job template",
        )
        jobs.append(
            seal_job({"schema_version": 2, **identity, **template}, repositories)
        )
    if len({job["id"] for job in jobs}) != len(jobs):
        raise ContractError("plan job identifiers are duplicated")
    blocked = plan["blocked"]
    if not isinstance(blocked, list) or len(blocked) > MAX_WORK:
        raise ContractError("blocked work is invalid")
    for item in blocked:
        _fields(item, {"component", "subject", "reason"}, "blocked work")
        for field in ("component", "subject", "reason"):
            _name(item[field], f"blocked {field}")
    return {"schema_version": 1, "jobs": jobs, "blocked": blocked}


def validate_result(value: Any, job: dict[str, Any]) -> dict[str, Any]:
    result = _fields(
        value,
        {
            "schema_version",
            "job_id",
            "manifest_digest",
            "status",
            "device",
            "cache",
            "duration_ms",
            "checks",
            "metrics",
        },
        "result",
    )
    if type(result["schema_version"]) is not int or result["schema_version"] != 2:
        raise ContractError("unsupported result version")
    if (
        result["job_id"] != job["id"]
        or result["manifest_digest"] != job["manifest_digest"]
    ):
        raise ContractError("result does not match job")
    if not isinstance(result["status"], str) or result["status"] not in STATUSES:
        raise ContractError("result status is invalid")
    device = _fields(result["device"], {"chip", "memory_gib"}, "device")
    _text(device["chip"], "device chip", 64)
    _int(device["memory_gib"], "device memory_gib", 512)
    if result["cache"] not in CACHE_RESULTS:
        raise ContractError("result cache is invalid")
    _nonnegative_int(result["duration_ms"], "duration_ms", 7 * 24 * 60 * 60 * 1000)
    checks = result["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= MAX_WORK:
        raise ContractError("result checks are invalid")
    for check in checks:
        _fields(check, {"name", "category", "status", "detail"}, "check")
        _text(check["name"], "check name", 80)
        if check["category"] not in CATEGORIES:
            raise ContractError("check category is invalid")
        if not isinstance(check["status"], str) or check["status"] not in STATUSES:
            raise ContractError("check status is invalid")
        _text(check["detail"], "check detail")
    metrics = result["metrics"]
    if not isinstance(metrics, list) or len(metrics) > MAX_WORK:
        raise ContractError("result metrics are invalid")
    for metric in metrics:
        _fields(
            metric,
            {"name", "unit", "base", "head", "change_pct", "verdict"},
            "metric",
        )
        _text(metric["name"], "metric name", 80)
        _text(metric["unit"], "metric unit", 16)
        for field in ("base", "head", "change_pct"):
            _number(metric[field], f"metric {field}")
        if metric["verdict"] not in METRIC_VERDICTS:
            raise ContractError("metric verdict is invalid")
    correctness_failed = any(
        check["category"] == "correctness" and check["status"] == "failed"
        for check in checks
    )
    if correctness_failed and any(
        metric["verdict"] != "advisory" for metric in metrics
    ):
        raise ContractError("metrics must be advisory after correctness failure")
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
