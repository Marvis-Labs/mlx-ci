import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mlx_ci.repository.executor import execute_phases, run

ROOT = Path(__file__).parents[2]


def work_args(tmp_path, phases):
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"phases": phases}))
    for name in ("control", "base", "head"):
        (tmp_path / name).mkdir()
    return argparse.Namespace(
        job=job,
        base=tmp_path / "base",
        head=tmp_path / "head",
        control=tmp_path / "control",
    )


def test_synthetic_failure_skips_hf_checkpoint(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, findings, control):
        calls.append(command)
        return 2, {"verdict": "test_failure", "error": "shape mismatch"}

    monkeypatch.setattr("mlx_ci.repository.executor._run", fake_run)
    output = tmp_path / "findings.json"
    monkeypatch.setenv("CI_JOB_FINDINGS", str(output))
    args = work_args(tmp_path, ["synthetic", "hf_checkpoint"])

    code, result = run(args, validate_execution=False)

    assert code == 2
    assert len(calls) == 1
    assert result["phases"]["synthetic"]["outcome"] == "test_failure"
    assert result["phases"]["hf_checkpoint"]["outcome"] == "skipped"
    assert json.loads(output.read_text()) == result


def test_hf_checkpoint_runs_only_after_synthetic_passes(monkeypatch, tmp_path):
    findings = iter(
        [
            (0, {"verdict": "passed", "correctness": {"match": True}}),
            (0, {"verdict": "improved", "correctness": {"match": True}}),
        ]
    )
    calls = []

    def fake_run(command, path, control):
        calls.append(command)
        return next(findings)

    monkeypatch.setattr("mlx_ci.repository.executor._run", fake_run)
    monkeypatch.setenv("CI_JOB_FINDINGS", str(tmp_path / "findings.json"))
    args = work_args(tmp_path, ["synthetic", "hf_checkpoint"])

    code, result = run(args, validate_execution=False)

    assert code == 0
    assert len(calls) == 2
    assert result["verdict"] == "improved"


def test_phase_environment_does_not_forward_runner_secrets(monkeypatch, tmp_path):
    findings = tmp_path / "phase.json"
    captured = {}

    def fake_subprocess(command, env):
        captured.update(env)
        findings.write_text('{"verdict":"passed"}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("mlx_ci.repository.executor.subprocess.run", fake_subprocess)
    monkeypatch.setenv("HF_TOKEN", "secret")
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("RUNNER_TOKEN", "secret")
    monkeypatch.setenv("PYTHONPATH", "/tmp/untrusted-python-path")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("CI_ASSETS_ROOT", "/trusted/assets")
    monkeypatch.setenv("CI_CHECKPOINTS_MANIFEST", "/trusted/checkpoints.json")

    from mlx_ci.repository.executor import _run

    code, _ = _run(["probe"], findings)

    assert code == 0
    assert captured["HF_HUB_OFFLINE"] == "1"
    assert captured["CI_ASSETS_ROOT"] == "/trusted/assets"
    assert captured["CI_CHECKPOINTS_MANIFEST"] == "/trusted/checkpoints.json"
    assert captured["PYTHONPATH"] == str(Path(__file__).resolve().parents[1])
    assert "HF_TOKEN" not in captured
    assert "GH_TOKEN" not in captured
    assert "RUNNER_TOKEN" not in captured


@pytest.mark.parametrize("settings", [{"PYTHONPATH": "untrusted"}, {"LIMIT": 3}])
def test_repository_environment_cannot_override_runtime(
    monkeypatch, tmp_path, settings
):
    from ci import plugin

    from mlx_ci.repository.executor import _run

    monkeypatch.setattr(plugin, "phase_environment", lambda: settings, raising=False)
    monkeypatch.setattr(
        "mlx_ci.repository.executor.subprocess.run",
        lambda *args, **kwargs: pytest.fail("executed with invalid environment"),
    )
    with pytest.raises(ValueError, match="repository phase environment"):
        _run(["probe"], tmp_path / "phase.json")


@pytest.mark.parametrize(
    "kind", ["missing", "symlink", "oversized", "duplicate", "array"]
)
def test_phase_rejects_invalid_findings(monkeypatch, tmp_path, kind):
    from mlx_ci.repository.executor import _run

    findings = tmp_path / "phase.json"

    def execute(command, env):
        if kind == "symlink":
            target = tmp_path / "other.json"
            target.write_text('{"verdict":"passed"}')
            findings.symlink_to(target)
        elif kind != "missing":
            findings.write_text(
                {
                    "oversized": " " * 1_048_577,
                    "duplicate": '{"verdict":"regressed","verdict":"passed"}',
                    "array": "[]",
                }[kind]
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("mlx_ci.repository.executor.subprocess.run", execute)
    code, result = _run(["phase"], findings)
    assert code == 2
    assert result["verdict"] == "test_failure"


def test_phase_never_reuses_findings_or_writes_inside_control(monkeypatch, tmp_path):
    from mlx_ci.repository.executor import _run

    monkeypatch.setattr(
        "mlx_ci.repository.executor.subprocess.run",
        lambda *args, **kwargs: pytest.fail("launched with an unsafe output path"),
    )
    findings = tmp_path / "existing.json"
    findings.write_text('{"verdict":"passed"}')
    with pytest.raises(FileExistsError):
        _run(["phase"], findings)
    with pytest.raises(ValueError, match="protected input"):
        _run(["phase"], tmp_path / "new.json", tmp_path)
    assert findings.read_text() == '{"verdict":"passed"}'


@pytest.mark.parametrize(
    "code,verdict,expected",
    [
        (0, "regressed", "regressed"),
        (1, "passed", "test_failure"),
        (0, "unknown", "test_failure"),
    ],
)
def test_shared_phase_loop_stops_on_failed_correctness(code, verdict, expected):
    calls = []

    def execute(name):
        calls.append(name)
        return code, {"verdict": verdict}

    result = execute_phases(["synthetic", "checkpoint", "performance"], execute)
    assert calls == ["synthetic"]
    assert result["verdict"] == expected
    assert result["phases"]["checkpoint"]["outcome"] == "skipped"
    assert result["phases"]["performance"]["outcome"] == "skipped"


@pytest.mark.parametrize("phases", [[], ["same", "same"], [None], "synthetic"])
def test_shared_phase_loop_rejects_ambiguous_requests(phases):
    with pytest.raises(ValueError, match="distinct named phases"):
        execute_phases(phases, lambda _: pytest.fail("executed invalid work"))
