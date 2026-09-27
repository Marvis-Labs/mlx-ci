from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from runners.contract import ContractError, read_json, seal_plan, validate_job
from runners.engines import load_engines
from runners.resources import MEMORY_TIERS_GIB, runner_decision, select_runner

RUNNER_GROUP = "marvis-apple-silicon"
BROKER = Path("/usr/local/libexec/marvis-ci/RUN_JOB.sh")


def memory_label(required_gib: int) -> str:
    if (
        isinstance(required_gib, bool)
        or not isinstance(required_gib, int)
        or required_gib < 1
    ):
        raise ContractError("required memory is invalid")
    for tier in MEMORY_TIERS_GIB:
        if required_gib <= tier:
            return f"memory-{tier}gb"
    raise ContractError("no runner memory tier can fit this job")


def admission_command(
    job_path: Path,
    result_path: Path,
    repositories: dict[str, str],
    runner: dict[str, Any],
) -> list[str]:
    job = validate_job(read_json(job_path), repositories)
    if not job_path.is_absolute() or not result_path.is_absolute():
        raise ContractError("runner paths must be absolute")
    memory_label(job["required_memory_gib"])
    decision = runner_decision(job, runner)
    if not decision["eligible"]:
        raise ContractError(f"runner refused job: {decision['reason']}")
    return [str(BROKER), str(job_path), str(result_path)]


def choose_runner(
    job: dict[str, Any],
    runners: list[dict[str, Any]],
    repositories: dict[str, str],
) -> dict[str, Any]:
    return select_runner(validate_job(job, repositories), runners)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("seal",))
    parser.add_argument("--attempt", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--engines", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    engines = load_engines(arguments.engines)
    repositories = {name: engine["repository"] for name, engine in engines.items()}
    jobs = seal_plan(
        read_json(arguments.attempt), read_json(arguments.plan), repositories
    )
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=arguments.output.parent, delete=False
    ) as stream:
        json.dump(jobs, stream, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
