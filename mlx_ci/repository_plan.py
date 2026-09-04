from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mlx_ci.contracts import ContractError, wrap_runner_manifest

MEMORY_CLASSES = (16, 32, 64, 128, 192, 256, 512)
FILE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json")


def prepare_repository_plan(
    value: Mapping[str, Any], *, jobs: Path
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    record = _record(value)
    devices = record.get("device_jobs")
    if not isinstance(devices, list) or len(devices) > 512:
        raise ContractError("repository plan device_jobs must be a bounded list")
    wrapped = []
    matrix = []
    seen = set()
    for device in devices:
        if not isinstance(device, Mapping) or set(device) not in (
            {"id", "file", "manifest"},
            {"id", "file", "memory_label", "manifest"},
        ):
            raise ContractError("repository plan device job is invalid")
        identifier = device.get("id")
        filename = device.get("file")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ContractError("repository plan device job id is invalid")
        if not isinstance(filename, str) or FILE_PATTERN.fullmatch(filename) is None:
            raise ContractError("repository plan device job file is invalid")
        seen.add(identifier)
        manifest = _manifest_file(jobs, filename, device.get("manifest"))
        queue_job = wrap_runner_manifest(manifest, attempt_id=str(record["attempt_id"]))
        if queue_job["job_id"] != identifier:
            raise ContractError("repository plan device job id does not match manifest")
        for field in ("repository", "base_sha", "head_sha", "contract_sha"):
            if queue_job[field] != record[field]:
                raise ContractError(
                    f"repository plan {field} does not match runner manifest"
                )
        label = _memory_label(queue_job["required_memory_gib"])
        legacy_label = device.get("memory_label")
        if legacy_label is not None and legacy_label != label:
            raise ContractError("repository plan memory label does not match manifest")
        wrapped.append(queue_job)
        matrix.append({"id": identifier, "file": filename, "memory_label": label})
    queue = {
        "schema_version": 1,
        "kind": "repository_queue",
        "attempt_id": record["attempt_id"],
        "repository": record["repository"],
        "base_sha": record["base_sha"],
        "head_sha": record["head_sha"],
        "contract_sha": record["contract_sha"],
        "terminal_state": record["terminal_state"],
        "jobs": wrapped,
    }
    return queue, matrix


def _record(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("repository plan must be an object")
    required = {
        "schema_version",
        "attempt_id",
        "repository",
        "base_sha",
        "head_sha",
        "contract_sha",
        "terminal_state",
        "device_jobs",
    }
    missing = sorted(required - set(value))
    if missing or value.get("schema_version") != 1:
        raise ContractError(f"repository plan is invalid; missing={missing}")
    if value.get("terminal_state") not in {"planned", "blocked"}:
        raise ContractError("repository plan terminal_state is invalid")
    return dict(value)


def _manifest_file(jobs: Path, filename: str, embedded: Any) -> dict[str, Any]:
    root = jobs.resolve(strict=True)
    if jobs.is_symlink() or not root.is_dir():
        raise ContractError("repository plan jobs directory is invalid")
    candidate = jobs / filename
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as error:
        raise ContractError("repository plan job file is invalid") from error
    if (
        candidate.is_symlink()
        or not resolved.is_file()
        or resolved.stat().st_size > 1_000_000
    ):
        raise ContractError("repository plan job file is invalid")
    try:
        persisted = json.loads(resolved.read_text())
    except (OSError, ValueError) as error:
        raise ContractError("repository plan job file is invalid") from error
    if not isinstance(embedded, Mapping) or persisted != embedded:
        raise ContractError("repository plan embedded manifest does not match its file")
    return dict(persisted)


def _memory_label(required: int) -> str:
    selected = next((value for value in MEMORY_CLASSES if value >= required), None)
    if selected is None:
        raise ContractError("job exceeds the largest runner memory class")
    return f"memory-{selected}gb"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    control = json.loads(args.control.read_text())
    queue, matrix = prepare_repository_plan(control, jobs=args.jobs)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(queue, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, args.output)
    if args.github_output is not None:
        with args.github_output.open("a") as stream:
            stream.write(f"has_work={'true' if matrix else 'false'}\n")
            stream.write("matrix=" + json.dumps({"include": matrix}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
