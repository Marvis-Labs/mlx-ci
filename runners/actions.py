from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from runners.contract import (
    ContractError,
    read_json,
    seal_plan,
    validate_attempt,
    validate_job,
    validate_result,
)
from runners.engines import load_engines
from runners.resources import MEMORY_TIERS_GIB


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


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def _repositories(engines: Path) -> dict[str, str]:
    return {
        name: engine["repository"] for name, engine in load_engines(engines).items()
    }


def matrix(
    jobs_document: dict[str, Any], repositories: dict[str, str]
) -> dict[str, Any]:
    if set(jobs_document) != {"schema_version", "jobs", "blocked"}:
        raise ContractError("jobs document fields are invalid")
    jobs = jobs_document["jobs"]
    if jobs_document["schema_version"] != 1 or not isinstance(jobs, list):
        raise ContractError("jobs document is invalid")
    include = []
    for job in jobs:
        validate_job(job, repositories)
        include.append(
            {
                "job_id": job["id"],
                "repository": job["repository"],
                "head_repository": job["head_repository"],
                "base_sha": job["base_sha"],
                "head_sha": job["head_sha"],
                "contract_sha": job["contract_sha"],
                "memory_label": memory_label(job["required_memory_gib"]),
            }
        )
    return {"include": include}


def extract_job(
    jobs_document: dict[str, Any], job_id: str, repositories: dict[str, str]
) -> dict[str, Any]:
    matches = [job for job in jobs_document.get("jobs", []) if job.get("id") == job_id]
    if len(matches) != 1:
        raise ContractError("job identifier is not unique")
    return validate_job(matches[0], repositories)


def normalize_result(
    job: dict[str, Any],
    raw: dict[str, Any] | None,
    chip: str,
    memory_gib: int,
    duration_ms: int,
) -> dict[str, Any]:
    device = {"chip": chip, "memory_gib": memory_gib}
    if raw is None:
        return validate_result(
            {
                "schema_version": 2,
                "job_id": job["id"],
                "manifest_digest": job["manifest_digest"],
                "status": "infrastructure_failure",
                "device": device,
                "cache": "not_applicable",
                "duration_ms": duration_ms,
                "checks": [
                    {
                        "name": "Runner",
                        "category": "infrastructure",
                        "status": "infrastructure_failure",
                        "detail": "Runner produced no result",
                    }
                ],
                "metrics": [],
            },
            job,
        )
    if not isinstance(raw, dict) or raw.get("job_id") != job["id"]:
        raise ContractError("runner result does not match job")
    outcome = raw.get("outcome")
    passed = outcome in {"passed", "improved"}
    infrastructure = outcome in {"declined", "infrastructure_failure"}
    status = (
        "passed" if passed else "infrastructure_failure" if infrastructure else "failed"
    )
    reason = raw.get("reason") or outcome or "unknown"
    cache_value = raw.get("cache", {})
    if isinstance(cache_value, dict):
        if cache_value.get("reused") is True:
            cache = "hit"
        elif cache_value.get("after") == "complete":
            cache = "downloaded"
        else:
            cache = "not_applicable"
    else:
        cache = "not_applicable"
    findings = raw.get("findings")
    metrics = findings.get("metrics", []) if isinstance(findings, dict) else []
    if not isinstance(metrics, list):
        metrics = []
    checks = findings.get("checks") if isinstance(findings, dict) else None
    if not isinstance(checks, list) or not checks or infrastructure:
        error = findings.get("error") if isinstance(findings, dict) else None
        if isinstance(error, str) and error:
            error_type = error.partition(":")[0]
            detail = {
                "CalledProcessError": "Checkout or test command failed",
                "ExecutionSecurityError": "Execution verification failed",
                "RuntimeError": "Model probe failed",
                "ValueError": "Test configuration failed validation",
            }.get(error_type, "Executor failed before producing checks")
        else:
            detail = str(reason).replace("_", " ")[:160]
        checks = [
            {
                "name": job["subject"],
                "category": "infrastructure" if infrastructure else "correctness",
                "status": status,
                "detail": detail,
            }
        ]
    result = {
        "schema_version": 2,
        "job_id": job["id"],
        "manifest_digest": job["manifest_digest"],
        "status": status,
        "device": device,
        "cache": cache,
        "duration_ms": duration_ms,
        "checks": checks,
        "metrics": metrics,
    }
    if status == "failed":
        for metric in result["metrics"]:
            metric["verdict"] = "advisory"
    return validate_result(result, job)


def collect_results(
    attempt: dict[str, Any],
    jobs_document: dict[str, Any],
    result_directory: Path,
    run_url: str,
    repositories: dict[str, str],
) -> dict[str, Any]:
    validate_attempt(attempt, repositories)
    results = []
    for job in jobs_document.get("jobs", []):
        validate_job(job, repositories)
        path = result_directory / f"{job['id']}.json"
        if path.is_file():
            result = validate_result(read_json(path), job)
        else:
            result = validate_result(
                {
                    "schema_version": 2,
                    "job_id": job["id"],
                    "manifest_digest": job["manifest_digest"],
                    "status": "infrastructure_failure",
                    "device": None,
                    "cache": "not_applicable",
                    "duration_ms": 0,
                    "checks": [
                        {
                            "name": "Runner",
                            "category": "infrastructure",
                            "status": "infrastructure_failure",
                            "detail": "No eligible runner reported a result",
                        }
                    ],
                    "metrics": [],
                },
                job,
            )
        results.append(result)
    return {
        "schema_version": 1,
        "attempt": attempt,
        "jobs": jobs_document,
        "results": results,
        "run_url": run_url,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("seal", "matrix", "job", "normalize", "collect")
    )
    parser.add_argument("--attempt", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--jobs", type=Path)
    parser.add_argument("--job-id")
    parser.add_argument("--raw", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--chip")
    parser.add_argument("--memory-gib", type=int)
    parser.add_argument("--started-ms", type=int)
    parser.add_argument("--run-url")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--engines", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    repositories = _repositories(arguments.engines)
    if arguments.command == "seal":
        value = seal_plan(
            read_json(arguments.attempt), read_json(arguments.plan), repositories
        )
    elif arguments.command == "matrix":
        value = matrix(read_json(arguments.jobs), repositories)
        if arguments.github_output:
            with arguments.github_output.open("a", encoding="utf-8") as stream:
                stream.write(f"matrix={json.dumps(value, separators=(',', ':'))}\n")
                stream.write(f"has_jobs={'true' if value['include'] else 'false'}\n")
    elif arguments.command == "job":
        value = extract_job(read_json(arguments.jobs), arguments.job_id, repositories)
    elif arguments.command == "normalize":
        job = read_json(arguments.jobs)
        raw = (
            read_json(arguments.raw)
            if arguments.raw and arguments.raw.is_file()
            else None
        )
        started = arguments.started_ms or int(time.time() * 1000)
        value = normalize_result(
            validate_job(job, repositories),
            raw,
            arguments.chip,
            arguments.memory_gib,
            max(0, int(time.time() * 1000) - started),
        )
    else:
        value = collect_results(
            read_json(arguments.attempt),
            read_json(arguments.jobs),
            arguments.results,
            arguments.run_url,
            repositories,
        )
    if arguments.output:
        _write(arguments.output, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
