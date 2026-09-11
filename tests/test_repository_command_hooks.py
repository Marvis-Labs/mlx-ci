import pytest

from mlx_ci.repository.adapter import main


@pytest.mark.parametrize("invalid", [None, "missing", "not_callable"])
def test_repository_command_hooks_are_complete_and_receive_parsed_identity(
    monkeypatch, tmp_path, invalid
):
    from ci import plugin

    calls = []

    def handler(args):
        calls.append(args)
        assert args.attempt_id == "attempt-1"
        assert args.head_sha == "a" * 40
        assert args.control == tmp_path / "control.json"
        return 0

    handlers = dict.fromkeys(("prepare", "plan", "hosted-checks", "report"), handler)
    if invalid == "missing":
        handlers.pop("prepare")
    elif invalid == "not_callable":
        handlers["prepare"] = None
    monkeypatch.setattr(plugin, "repository_handlers", lambda: handlers, raising=False)
    arguments = [
        "report",
        "--control",
        str(tmp_path / "control.json"),
        "--results",
        str(tmp_path / "results"),
        "--output",
        str(tmp_path / "summary.md"),
        "--attempt-id",
        "attempt-1",
        "--head-sha",
        "a" * 40,
        "--run-url",
        "https://example.invalid/run",
    ]
    if invalid:
        with pytest.raises(ValueError, match="all command handlers"):
            main(arguments)
        assert calls == []
    else:
        assert main(arguments) == 0
        assert len(calls) == 1
