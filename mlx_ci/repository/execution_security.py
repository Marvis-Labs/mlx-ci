from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
)


class ExecutionSecurityError(ValueError):
    pass


def canonical_digest(value: Mapping[str, Any]) -> str:
    value = _json_object(value, "manifest")
    payload = json.dumps(
        value, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def seal_job(
    job: Mapping[str, Any],
    *,
    repository: str,
    base_sha: str,
    head_sha: str,
    contract_sha: str,
) -> dict[str, Any]:
    sealed = _json_object(job, "work")
    sealed.update(
        {
            "repository": repository,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "contract_sha": contract_sha,
        }
    )
    sealed.pop("manifest_digest", None)
    validate_job(sealed, require_digest=False)
    sealed["manifest_digest"] = canonical_digest(sealed)
    return sealed


def validate_job(job: Mapping[str, Any], *, require_digest: bool = True) -> None:
    _json_object(job, "manifest")
    for field in ("id", "work_type", "component", "subject"):
        if not isinstance(job.get(field), str) or not job[field]:
            raise ExecutionSecurityError(f"work manifest requires {field}")
    repository = job.get("repository")
    if (
        not isinstance(repository, str)
        or REPOSITORY_PATTERN.fullmatch(repository) is None
    ):
        raise ExecutionSecurityError("work manifest requires repository owner/name")
    phases = job.get("phases")
    if not isinstance(phases, list) or not phases:
        raise ExecutionSecurityError("work manifest requires phases")
    if len(phases) != len(set(phases)) or any(
        not isinstance(phase, str) or not 1 <= len(phase) <= 128 for phase in phases
    ):
        raise ExecutionSecurityError("work manifest contains invalid phases")
    for field in ("required_memory_gib", "required_disk_gib"):
        if type(job.get(field)) is not int or job[field] <= 0:
            raise ExecutionSecurityError(f"work manifest requires positive {field}")
    for field in ("base_sha", "head_sha", "contract_sha"):
        value = job.get(field)
        if not isinstance(value, str) or COMMIT_PATTERN.fullmatch(value) is None:
            raise ExecutionSecurityError(f"work manifest requires immutable {field}")
    if require_digest:
        supplied = job.get("manifest_digest")
        unsigned = dict(job)
        unsigned.pop("manifest_digest", None)
        if supplied != canonical_digest(unsigned):
            raise ExecutionSecurityError("work manifest digest does not match")

    try:
        from ci.plugin import validate_job as validate_participant_job

        validate_participant_job(job)
    except (ImportError, TypeError, ValueError) as error:
        raise ExecutionSecurityError(str(error)) from error


def verify_execution(
    job: Mapping[str, Any],
    *,
    control: Path,
    base: Path,
    head: Path,
    commands: Mapping[str, Sequence[str]] | None = None,
    entrypoint: Path | None = None,
) -> None:
    validate_job(job)
    require_checkout(control, str(job["contract_sha"]), "control")
    require_checkout(base, str(job["base_sha"]), "base")
    require_checkout(head, str(job["head_sha"]), "head")
    if entrypoint is not None:
        if commands is not None:
            raise ExecutionSecurityError("provide commands or one entrypoint, not both")
        verify_trusted_file(control, entrypoint)
        return
    if commands is None:
        raise ExecutionSecurityError("execution requires trusted commands")
    for phase in job["phases"]:
        command = commands.get(phase)
        if command is None or len(command) < 2:
            raise ExecutionSecurityError(f"phase has no trusted command: {phase}")
        verify_trusted_file(control, Path(command[1]))


def require_checkout(repository: Path, expected: str, role: str) -> None:
    if COMMIT_PATTERN.fullmatch(expected) is None:
        raise ExecutionSecurityError(f"{role} requires an immutable revision")
    resolved = repository.resolve(strict=True)
    if repository.is_symlink() or not resolved.is_dir():
        raise ExecutionSecurityError(f"{role} checkout is not a real directory")
    actual = _git(repository, "rev-parse", "--verify", "HEAD^{commit}")
    if actual != expected:
        raise ExecutionSecurityError(
            f"{role} checkout is {actual or 'unknown'}, expected {expected}"
        )
    status = _git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if status:
        raise ExecutionSecurityError(f"{role} checkout is not clean")


def verify_trusted_file(control: Path, path: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(control.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise ExecutionSecurityError(
            "phase entry point is outside trusted control"
        ) from error
    if path.is_symlink() or relative.parts[:1] != ("ci",):
        raise ExecutionSecurityError("phase entry point is not trusted CI code")
    expected = _git(control, "show", f"HEAD:{relative.as_posix()}", raw=True)
    if not isinstance(expected, bytes) or resolved.read_bytes() != expected:
        raise ExecutionSecurityError("phase entry point differs from control commit")


def _git(repository: Path, *arguments: str, raw: bool = False) -> str | bytes:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={repository.resolve(strict=True)}", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=not raw,
    )
    return completed.stdout if raw else completed.stdout.strip()


def _json_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ExecutionSecurityError(f"{name} must be an object with string keys")
    _validate_json_value(value, name)
    return json.loads(json.dumps(value, allow_nan=False))


def _validate_json_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExecutionSecurityError(f"{path} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ExecutionSecurityError(f"{path} contains a non-string key")
            _validate_json_value(nested, f"{path}.{key}")
        return
    if isinstance(value, list | tuple):
        for index, nested in enumerate(value):
            _validate_json_value(nested, f"{path}[{index}]")
        return
    raise ExecutionSecurityError(f"{path} contains a non-JSON value")
