import json
import sys

import pytest

from mlx_ci.repository.isolated_probe import ProbeProcessError, run_project_probe


def test_controller_launcher_ignores_project_python_startup(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "imported-untrusted-code"
    (project / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    (project / "json.py").write_text("raise RuntimeError('untrusted import')\n")
    launcher = tmp_path / "launcher.py"
    launcher.write_text("import json\nprint(json.dumps({'isolated': True}))\n")
    probe = tmp_path / "probe.py"
    probe.touch()
    for name, value in {
        "CI_NETWORK_DISABLED": "1",
        "CI_AUDIO_OUTPUT_DISABLED": "1",
        "CI_REQUIRE_SANDBOX": "1",
        "CI_JOB_PYTHON": sys.executable,
        "CI_PROBE_RUNNER": str(launcher),
        "CI_PROBE_CONTEXT": str(tmp_path / "context.json"),
    }.items():
        monkeypatch.setenv(name, value)
    assert json.loads(run_project_probe(project, probe, [])) == {"isolated": True}
    assert not marker.exists()


def test_runner_mode_never_falls_back_to_controller_execution(monkeypatch, tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text("")
    monkeypatch.setenv("CI_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CI_AUDIO_OUTPUT_DISABLED", "1")
    monkeypatch.setenv("CI_REQUIRE_SANDBOX", "1")
    monkeypatch.delenv("CI_PROBE_RUNNER", raising=False)
    with pytest.raises(ProbeProcessError, match="isolated probe runner"):
        run_project_probe(tmp_path, probe, [])


def test_runner_mode_delegates_only_to_probe_boundary(monkeypatch, tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text("")
    for name, value in {
        "CI_NETWORK_DISABLED": "1",
        "CI_AUDIO_OUTPUT_DISABLED": "1",
        "CI_REQUIRE_SANDBOX": "1",
        "CI_JOB_PYTHON": "/runtime/python",
        "CI_PROBE_RUNNER": "/runner/PROBE_RUNNER.py",
        "CI_PROBE_CONTEXT": "/input/probes.json",
    }.items():
        monkeypatch.setenv(name, value)
    observed = []
    monkeypatch.setattr(
        "mlx_ci.repository.isolated_probe._run_bounded",
        lambda command, **kwargs: observed.append(command) or b"{}",
    )
    assert run_project_probe(tmp_path, probe, ["--job", "/input/job.json"]) == b"{}"
    assert observed == [
        [
            "/runtime/python",
            "-I",
            "/runner/PROBE_RUNNER.py",
            "--context",
            "/input/probes.json",
            "--project",
            str(tmp_path),
            "--probe",
            str(probe),
            "--",
            "--job",
            "/input/job.json",
        ]
    ]


def test_probe_process_requires_runner_isolation(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text("")
    monkeypatch.delenv("CI_NETWORK_DISABLED", raising=False)
    monkeypatch.setenv("CI_AUDIO_OUTPUT_DISABLED", "1")

    with pytest.raises(ProbeProcessError, match="network isolation"):
        run_project_probe(project, probe, [])


def test_probe_process_scrubs_environment(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os\n"
        "print(json.dumps({'secret': os.getenv('SECRET_VALUE'), "
        "'hf_home': os.getenv('HF_HOME'), "
        "'offline': os.getenv('HF_HUB_OFFLINE'), "
        "'usersite': os.getenv('PYTHONNOUSERSITE')}))\n"
    )
    monkeypatch.setenv("CI_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CI_AUDIO_OUTPUT_DISABLED", "1")
    monkeypatch.setenv("CI_JOB_PYTHON", "/usr/bin/python3")
    monkeypatch.setenv("SECRET_VALUE", "never-forward")
    monkeypatch.setenv("HF_HOME", "/shared/cache")
    result = json.loads(run_project_probe(project, probe, []))
    assert result == {
        "secret": None,
        "hf_home": None,
        "offline": "1",
        "usersite": "1",
    }


def test_probe_process_does_not_expose_child_output_on_failure(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\n"
        "sys.stdout.write('untrusted audio')\n"
        "sys.stderr.write('untrusted secrets')\n"
        "raise SystemExit(7)\n"
    )
    monkeypatch.setenv("CI_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CI_AUDIO_OUTPUT_DISABLED", "1")
    monkeypatch.setenv("CI_JOB_PYTHON", "/usr/bin/python3")

    with pytest.raises(ProbeProcessError, match="status 7") as error:
        run_project_probe(project, probe, [])
    assert "untrusted" not in str(error.value)


def test_probe_process_bounds_untrusted_output(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text("import sys\nsys.stdout.buffer.write(b'x' * 17_000_000)\n")
    monkeypatch.setenv("CI_NETWORK_DISABLED", "1")
    monkeypatch.setenv("CI_AUDIO_OUTPUT_DISABLED", "1")
    monkeypatch.setenv("CI_JOB_PYTHON", "/usr/bin/python3")

    with pytest.raises(ProbeProcessError, match="size limit"):
        run_project_probe(project, probe, [], timeout_seconds=5)
