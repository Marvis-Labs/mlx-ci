from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

SCHEMA_VERSION = 1
REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
)
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")


class ContractError(ValueError):
    pass


def canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            _json_value(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except ContractError:
        raise
    except (RecursionError, TypeError, ValueError) as error:
        raise ContractError("contract must contain canonical JSON values") from error


def canonical_digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def validate_request(value: Mapping[str, Any]) -> dict[str, Any]:
    _exact_fields(
        value,
        {
            "schema_version",
            "kind",
            "request_id",
            "repository",
            "pull_request",
            "comment_id",
            "requester",
            "requested_at",
        },
    )
    _header(value, "run_request")
    _identifier(value, "request_id")
    _repository(value, "repository")
    _positive_integer(value, "pull_request")
    _positive_integer(value, "comment_id")
    _identifier(value, "requester")
    _timestamp(value, "requested_at")
    return dict(value)


def seal_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    manifest = dict(value)
    manifest.pop("manifest_digest", None)
    validate_job(manifest, require_digest=False)
    manifest["manifest_digest"] = canonical_digest(manifest)
    return manifest


def validate_job(
    value: Mapping[str, Any], *, require_digest: bool = True
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "job_id",
        "attempt_id",
        "repository",
        "base_sha",
        "head_sha",
        "contract_sha",
        "component",
        "subject",
        "phases",
        "required_memory_gib",
        "required_disk_gib",
        "payload",
    }
    if require_digest:
        fields.add("manifest_digest")
    _exact_fields(value, fields)
    _header(value, "work_manifest")
    for field in ("job_id", "attempt_id", "component", "subject"):
        _identifier(value, field)
    _repository(value, "repository")
    for field in ("base_sha", "head_sha", "contract_sha"):
        _commit(value, field)
    phases = value.get("phases")
    if (
        not isinstance(phases, list)
        or not phases
        or len(phases) > 16
        or len(phases) != len(set(phases))
    ):
        raise ContractError("phases must be a non-empty unique list")
    for phase in phases:
        if not isinstance(phase, str) or LABEL_PATTERN.fullmatch(phase) is None:
            raise ContractError("phase name is invalid")
    _positive_integer(value, "required_memory_gib")
    _positive_integer(value, "required_disk_gib")
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        raise ContractError("payload must be an object")
    if len(canonical_json(payload)) > 1_000_000:
        raise ContractError("payload exceeds the contract size limit")
    if require_digest:
        _digest(value, "manifest_digest")
        unsigned = dict(value)
        supplied = unsigned.pop("manifest_digest")
        if supplied != canonical_digest(unsigned):
            raise ContractError("manifest_digest does not match")
    return dict(value)


def validate_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    _exact_fields(
        value,
        {
            "schema_version",
            "kind",
            "algorithm",
            "key_id",
            "manifest",
            "signature",
        },
    )
    _header(value, "signed_work_manifest")
    if value.get("algorithm") != "ed25519":
        raise ContractError("unsupported signature algorithm")
    _identifier(value, "key_id")
    manifest = value.get("manifest")
    if not isinstance(manifest, Mapping):
        raise ContractError("signed envelope requires a manifest")
    validate_job(manifest)
    signature = value.get("signature")
    if not isinstance(signature, str):
        raise ContractError("signature must be base64")
    try:
        decoded = base64.b64decode(signature, validate=True)
    except ValueError as error:
        raise ContractError("signature must be base64") from error
    if len(decoded) != 64:
        raise ContractError("ed25519 signature must contain 64 bytes")
    return dict(value)


def validate_runner(value: Mapping[str, Any]) -> dict[str, Any]:
    _exact_fields(
        value,
        {
            "schema_version",
            "kind",
            "runner_id",
            "labels",
            "memory_gib",
            "available_disk_gib",
            "status",
            "heartbeat_at",
        },
    )
    _header(value, "runner_capability")
    _identifier(value, "runner_id")
    labels = value.get("labels")
    if (
        not isinstance(labels, list)
        or not labels
        or len(labels) > 32
        or len(labels) != len(set(labels))
    ):
        raise ContractError("runner labels must be a non-empty unique list")
    if any(
        not isinstance(label, str) or LABEL_PATTERN.fullmatch(label) is None
        for label in labels
    ):
        raise ContractError("runner label is invalid")
    _positive_integer(value, "memory_gib")
    _non_negative_integer(value, "available_disk_gib")
    if value.get("status") not in {"online", "draining", "offline"}:
        raise ContractError("runner status is invalid")
    _timestamp(value, "heartbeat_at")
    return dict(value)


def validate_lease(value: Mapping[str, Any]) -> dict[str, Any]:
    _exact_fields(
        value,
        {
            "schema_version",
            "kind",
            "lease_id",
            "attempt_id",
            "job_id",
            "runner_id",
            "generation",
            "acquired_at",
            "heartbeat_at",
            "expires_at",
        },
    )
    _header(value, "runner_lease")
    for field in ("lease_id", "attempt_id", "job_id", "runner_id", "generation"):
        _identifier(value, field)
    acquired = _timestamp(value, "acquired_at")
    heartbeat = _timestamp(value, "heartbeat_at")
    expires = _timestamp(value, "expires_at")
    if not acquired <= heartbeat < expires:
        raise ContractError("lease timestamps are not ordered")
    return dict(value)


def seal_result(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("result_digest", None)
    validate_result(result, require_digest=False)
    result["result_digest"] = canonical_digest(result)
    return result


def validate_result(
    value: Mapping[str, Any], *, require_digest: bool = True
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "kind",
        "job_id",
        "attempt_id",
        "repository",
        "runner_id",
        "lease_id",
        "outcome",
        "failure_code",
        "evidence",
        "started_at",
        "finished_at",
    }
    if require_digest:
        fields.add("result_digest")
    _exact_fields(value, fields)
    _header(value, "work_result")
    for field in ("job_id", "attempt_id", "runner_id", "lease_id"):
        _identifier(value, field)
    _repository(value, "repository")
    if value.get("outcome") not in {
        "passed",
        "improved",
        "regressed",
        "test_failure",
        "infrastructure_failure",
        "declined",
        "cancelled",
    }:
        raise ContractError("result outcome is invalid")
    failure_code = value.get("failure_code")
    if failure_code is not None and (
        not isinstance(failure_code, str)
        or LABEL_PATTERN.fullmatch(failure_code) is None
    ):
        raise ContractError("failure_code is invalid")
    evidence = value.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ContractError("result evidence must be an object")
    if len(canonical_json(evidence)) > 2_000_000:
        raise ContractError("result evidence exceeds the contract size limit")
    started = _timestamp(value, "started_at")
    finished = _timestamp(value, "finished_at")
    if started > finished:
        raise ContractError("result timestamps are not ordered")
    if require_digest:
        _digest(value, "result_digest")
        unsigned = dict(value)
        supplied = unsigned.pop("result_digest")
        if supplied != canonical_digest(unsigned):
            raise ContractError("result_digest does not match")
    return dict(value)


def _exact_fields(value: Mapping[str, Any], expected: set[str]) -> None:
    if not isinstance(value, Mapping):
        raise ContractError("contract must be an object")
    missing = sorted(expected - set(value))
    unexpected = sorted(set(value) - expected)
    if missing or unexpected:
        raise ContractError(
            f"contract fields differ; missing={missing}, unexpected={unexpected}"
        )


def _header(value: Mapping[str, Any], kind: str) -> None:
    if value.get("schema_version") != SCHEMA_VERSION or value.get("kind") != kind:
        raise ContractError(f"contract must be {kind} schema v{SCHEMA_VERSION}")


def _identifier(value: Mapping[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or IDENTIFIER_PATTERN.fullmatch(item) is None:
        raise ContractError(f"{field} is invalid")
    return item


def _repository(value: Mapping[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or REPOSITORY_PATTERN.fullmatch(item) is None:
        raise ContractError(f"{field} must use owner/name format")
    return item


def _commit(value: Mapping[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or COMMIT_PATTERN.fullmatch(item) is None:
        raise ContractError(f"{field} must be a full lowercase commit SHA")
    return item


def _digest(value: Mapping[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or DIGEST_PATTERN.fullmatch(item) is None:
        raise ContractError(f"{field} is invalid")
    return item


def _positive_integer(value: Mapping[str, Any], field: str) -> int:
    item = value.get(field)
    if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
        raise ContractError(f"{field} must be a positive integer")
    return item


def _non_negative_integer(value: Mapping[str, Any], field: str) -> int:
    item = value.get(field)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise ContractError(f"{field} must be a non-negative integer")
    return item


def _timestamp(value: Mapping[str, Any], field: str) -> datetime:
    item = value.get(field)
    if not isinstance(item, str) or TIMESTAMP_PATTERN.fullmatch(item) is None:
        raise ContractError(f"{field} must be a UTC RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(item.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ContractError(f"{field} must be a UTC RFC3339 timestamp") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ContractError(f"{field} must be a UTC RFC3339 timestamp")
    return parsed


def _json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 64:
        raise ContractError("contract JSON exceeds the nesting limit")
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("contract JSON numbers must be finite")
        return value
    if isinstance(value, list):
        return [_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContractError("contract JSON object keys must be strings")
        return {key: _json_value(item, depth=depth + 1) for key, item in value.items()}
    raise ContractError("contract must contain canonical JSON values")
