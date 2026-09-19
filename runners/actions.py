from __future__ import annotations

from pathlib import Path

from runners.contract import ContractError, read_json, validate_job


MEMORY_TIERS = (16, 32, 64, 128, 256, 512)
RUNNER_GROUP = "marvis-apple-silicon"
BROKER = Path("/usr/local/libexec/marvis-ci/RUN_JOB.sh")


def memory_label(required_gib: int) -> str:
    if (
        isinstance(required_gib, bool)
        or not isinstance(required_gib, int)
        or required_gib < 1
    ):
        raise ContractError("required memory is invalid")
    for tier in MEMORY_TIERS:
        if required_gib <= tier:
            return f"memory-{tier}gb"
    raise ContractError("no runner memory tier can fit this job")


def admission_command(
    job_path: Path, result_path: Path, repositories: dict[str, str]
) -> list[str]:
    job = validate_job(read_json(job_path), repositories)
    if not job_path.is_absolute() or not result_path.is_absolute():
        raise ContractError("runner paths must be absolute")
    memory_label(job["required_memory_gib"])
    return [str(BROKER), str(job_path), str(result_path)]
