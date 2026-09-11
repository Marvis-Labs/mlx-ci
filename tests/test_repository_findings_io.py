import json
import stat

import pytest

from mlx_ci.repository.findings_io import write_findings


def test_findings_are_exclusive_and_private(tmp_path):
    output = tmp_path / "result.json"
    write_findings(output, {"verdict": "passed"}, protected=())
    assert json.loads(output.read_text()) == {"verdict": "passed"}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_findings(output, {"verdict": "regressed"}, protected=())
    assert json.loads(output.read_text())["verdict"] == "passed"


def test_findings_cannot_overwrite_symlink_or_protected_tree(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    sentinel = source / "input"
    sentinel.write_text("keep")
    output = tmp_path / "result.json"
    output.symlink_to(sentinel)
    with pytest.raises(FileExistsError):
        write_findings(output, {}, protected=(source,))
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="protected"):
        write_findings(alias / "new.json", {}, protected=(source,))
    assert sentinel.read_text() == "keep"
    assert not (source / "new.json").exists()


@pytest.mark.parametrize(
    "result", [{"value": float("nan")}, {"value": "x" * 1_048_576}]
)
def test_invalid_findings_do_not_create_a_file(tmp_path, result):
    output = tmp_path / "result.json"
    with pytest.raises(ValueError):
        write_findings(output, result, protected=())
    assert not output.exists()
