from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from mlx_ci.repository import execution_security as security
from mlx_ci.repository.components import ExecutionContext
from mlx_ci.repository.findings_io import (
    load_record,
    validate_findings_path,
    write_findings,
)

FORWARDED_ENVIRONMENT = frozenset(
    {
        "CI_ASSETS_MANIFEST",
        "CI_ASSETS_ROOT",
        "CI_CHECKPOINT_PATH",
        "CI_CHECKPOINTS_MANIFEST",
        "CI_REQUIRE_SANDBOX",
        "CI_JOB_PYTHON",
        "CI_PROBE_RUNNER",
        "CI_PROBE_CONTEXT",
        "HF_ASSETS_CACHE",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HF_HUB_OFFLINE",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "TMPDIR",
        "TRANSFORMERS_OFFLINE",
        "UV_CACHE_DIR",
        "UV_OFFLINE",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PROJECT_ENVIRONMENT",
    }
)


def _run(
    command: list[str], findings: Path, control: Path | None = None
) -> tuple[int, dict[str, Any]]:
    validate_findings_path(findings, protected=(control,) if control else ())
    environment = {
        key: value for key, value in os.environ.items() if key in FORWARDED_ENVIRONMENT
    }
    environment["CI_JOB_FINDINGS"] = str(findings)
    from ci import plugin

    configure = getattr(plugin, "phase_environment", None)
    if configure is not None:
        additions = configure()
        if (
            not isinstance(additions, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in additions.items()
            )
            or set(additions) & {"PYTHONPATH", "PATH", "HOME", "CI_JOB_FINDINGS"}
        ):
            raise ValueError("invalid repository phase environment")
        environment.update(additions)
    paths = [Path(__file__).resolve().parents[2]]
    if control is not None:
        paths.append(control)
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in paths)
    completed = subprocess.run(command, env=environment)
    try:
        value = load_record(findings)
    except (ValueError, OSError) as error:
        return completed.returncode or 2, {
            "verdict": "test_failure",
            "error": f"invalid phase findings: {error}"[:2048],
        }
    return completed.returncode, value


def _phase(findings: Mapping[str, Any], returncode: int) -> dict[str, Any]:
    verdict = str(findings.get("verdict", "test_failure"))
    outcome = (
        verdict if verdict in {"passed", "improved", "regressed"} else "test_failure"
    )
    if returncode and outcome != "test_failure":
        outcome = "test_failure"
    return {"outcome": outcome, "findings": dict(findings)}


def execute_phases(
    requested: Sequence[str],
    execute: Callable[[str], tuple[int, dict[str, Any]]],
) -> dict[str, Any]:
    if (
        not requested
        or isinstance(requested, str | bytes)
        or any(not isinstance(name, str) or not name for name in requested)
        or len(set(requested)) != len(requested)
    ):
        raise ValueError("execution requires distinct named phases")
    phases = {}
    for index, name in enumerate(requested):
        code, findings = execute(name)
        phases[name] = _phase(findings, code)
        if phases[name]["outcome"] not in {"passed", "improved"}:
            for pending in requested[index + 1 :]:
                phases[pending] = {
                    "outcome": "skipped",
                    "findings": {"reason": f"{name}_failed"},
                }
            break
    outcomes = {phase["outcome"] for phase in phases.values()}
    verdict = next(
        (
            value
            for value in ("test_failure", "regressed", "improved")
            if value in outcomes
        ),
        "passed",
    )
    return {"verdict": verdict, "phases": phases}


def _failure(error: Exception) -> dict[str, Any]:
    return {
        "verdict": "test_failure",
        "error": f"{type(error).__name__}: {error}"[:2048],
    }


def run(
    args: argparse.Namespace, *, validate_execution: bool = True
) -> tuple[int, dict[str, Any]]:
    from ci import plugin

    output = Path(os.environ.get("CI_JOB_FINDINGS", "findings.json"))
    protected = [args.control, args.base, args.head, args.job]
    protected.extend(
        Path(os.environ[key])
        for key in (
            "CI_ASSETS_ROOT",
            "CI_ASSETS_MANIFEST",
            "CI_CHECKPOINTS_MANIFEST",
            "CI_CHECKPOINT_PATH",
        )
        if os.environ.get(key)
    )
    try:
        inputs = getattr(plugin, "protected_inputs", None)
        if inputs is not None:
            protected.extend(inputs())
        validate_findings_path(output, protected=protected)
        code, result = _execute(
            args, output, protected=protected, validate_execution=validate_execution
        )
    except Exception as error:
        code, result = 2, _failure(error)
    try:
        write_findings(output, result, protected=protected)
    except Exception as error:
        return 2, _failure(error)
    return code, result


def _execute(args, output, *, protected, validate_execution=True):
    from ci import plugin

    job = load_record(args.job)
    if validate_execution:
        security.validate_job(job)

    def execute(name):
        phase_output = output.with_name(f"{output.stem}-{name}.json")
        validate_findings_path(phase_output, protected=protected)
        context = ExecutionContext(
            job_path=args.job,
            control=args.control,
            base=args.base,
            head=args.head,
            output=phase_output,
        )
        commands = plugin.phase_commands(context)
        if name not in commands:
            raise ValueError(f"unsupported work phase: {name}")
        if validate_execution:
            security.verify_execution(
                job,
                control=args.control,
                base=args.base,
                head=args.head,
                commands=commands,
            )
        code, findings = _run(commands[name], phase_output, args.control)
        validator = getattr(plugin, "validate_phase", None)
        if validator is not None:
            validator(job, name, findings)
        return code, findings

    result = execute_phases(job["phases"], execute)
    result["execution"] = {
        key: job[key]
        for key in (
            "repository",
            "base_sha",
            "head_sha",
            "contract_sha",
            "manifest_digest",
        )
        if key in job
    }
    return (0 if result["verdict"] in {"passed", "improved"} else 2), result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    args = parser.parse_args(argv)
    return run(args)[0]


if __name__ == "__main__":
    raise SystemExit(main())
