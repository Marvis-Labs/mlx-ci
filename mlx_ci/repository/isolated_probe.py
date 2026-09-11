from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path


class ProbeProcessError(RuntimeError):
    pass


def run_project_probe(
    project: Path,
    probe: Path,
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: int = 900,
) -> bytes:
    if os.environ.get("CI_NETWORK_DISABLED") != "1":
        raise ProbeProcessError("probe execution requires network isolation")
    if os.environ.get("CI_AUDIO_OUTPUT_DISABLED") != "1":
        raise ProbeProcessError("probe execution requires audio-output isolation")
    if timeout_seconds <= 0 or timeout_seconds > 3600:
        raise ProbeProcessError("probe timeout is outside the allowed range")

    project_root = project.resolve(strict=True)
    probe_path = probe.resolve(strict=True)
    if project.is_symlink() or not project_root.is_dir() or probe.is_symlink():
        raise ProbeProcessError("probe paths must be real files and directories")

    job_python = os.environ.get("CI_JOB_PYTHON")
    launcher = os.environ.get("CI_PROBE_RUNNER")
    context = os.environ.get("CI_PROBE_CONTEXT")
    if launcher and context and job_python:
        command = [
            job_python,
            "-I",
            launcher,
            "--context",
            context,
            "--project",
            str(project_root),
            "--probe",
            str(probe_path),
            "--",
            *arguments,
        ]
    elif os.environ.get("CI_REQUIRE_SANDBOX") == "1":
        raise ProbeProcessError("sandbox execution requires the isolated probe runner")
    elif job_python:
        command = [job_python, str(probe_path), *arguments]
    else:
        command = [
            "uv",
            "run",
            "--frozen",
            "--offline",
            "--project",
            str(project_root),
            "--python",
            "3.10",
            "python",
            str(probe_path),
            *arguments,
        ]

    allowed_environment = {
        name: os.environ[name]
        for name in (
            "LANG",
            "LC_ALL",
            "MLX_METAL_CACHE_DIR",
            "PATH",
            "TMPDIR",
            "UV_CACHE_DIR",
        )
        if name in os.environ
    }
    allowed_environment.update(
        {
            "CI_AUDIO_OUTPUT_DISABLED": "1",
            "CI_NETWORK_DISABLED": "1",
            "HF_DATASETS_OFFLINE": "1",
            "HF_HUB_OFFLINE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(project_root),
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    if environment:
        unexpected = sorted(
            set(environment)
            - {
                "MLX_METAL_PREWARM",
                "TOKENIZERS_PARALLELISM",
            }
        )
        if unexpected:
            raise ProbeProcessError(
                "probe environment contains unsupported fields: "
                + ", ".join(unexpected)
            )
        allowed_environment.update(environment)

    return _run_bounded(
        command,
        cwd=project_root,
        env=allowed_environment,
        timeout_seconds=timeout_seconds,
        new_session=not bool(launcher and context and job_python),
    )


def _run_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    new_session: bool = True,
) -> bytes:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=new_session,
    )
    if process.stdout is None:
        _terminate(process, new_session)
        raise ProbeProcessError("project probe has no captured output")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    output = bytearray()
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process, new_session)
                raise ProbeProcessError("project probe exceeded its timeout")
            events = selector.select(min(remaining, 1.0))
            if not events and process.poll() is not None:
                events = [(selector.get_key(process.stdout), selectors.EVENT_READ)]
            for key, _ in events:
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > 16_777_216:
                    _terminate(process, new_session)
                    raise ProbeProcessError(
                        "project probe output exceeds the size limit"
                    )
        remaining = max(0.01, deadline - time.monotonic())
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as error:
        _terminate(process, new_session)
        raise ProbeProcessError("project probe exceeded its timeout") from error
    finally:
        selector.close()
        process.stdout.close()
    if returncode != 0:
        raise ProbeProcessError(f"project probe exited with status {returncode}")
    return bytes(output)


def _terminate(process: subprocess.Popen[bytes], process_group: bool = True) -> None:
    if process.poll() is None:
        try:
            if process_group:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
    process.wait()
