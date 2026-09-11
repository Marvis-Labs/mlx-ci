from __future__ import annotations

import argparse
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from mlx_ci.repository import control, hosted_checks, report
from mlx_ci.repository.component_config import materialize


def _identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository-path", type=Path, required=True)
    parser.add_argument("--base-checkout", type=Path, required=True)
    parser.add_argument("--head-checkout", type=Path, required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)


def _prepare(args: argparse.Namespace) -> int:
    control.export_repository_plan(
        repository_path=args.repository_path,
        base_checkout=args.base_checkout,
        head_checkout=args.head_checkout,
        base_sha=args.base_sha,
        head_sha=args.head_sha,
        contract_sha=args.contract_sha,
        repository=args.repository,
        pr_number=args.pr_number,
        attempt_id=args.attempt_id,
        run_url=args.run_url,
        output=args.output,
        jobs=args.jobs,
    )
    return 0


def _plan(args: argparse.Namespace) -> int:
    control.validate_export_identity(
        args.base_sha,
        args.head_sha,
        args.contract_sha,
        args.repository,
        args.pr_number,
        "planning",
    )
    control.import_checkout(
        args.repository_path, args.base_checkout, args.base_sha, "base"
    )
    control.import_checkout(
        args.repository_path, args.head_checkout, args.head_sha, "head"
    )
    with tempfile.TemporaryDirectory(prefix="repository-plan-") as temporary:
        configuration = Path(temporary)
        materialize(args.repository_path, args.head_sha, configuration)
        control.plan_repository(
            repository_path=args.repository_path,
            base=args.base_sha,
            head=args.head_sha,
            repository=args.repository,
            contract_sha=args.contract_sha,
            pr_number=args.pr_number,
            rules_config=args.repository_path / "ci" / "config/changes.yaml",
            component_config_directory=configuration,
            protected_config=(
                args.repository_path / "ci" / "config/protected-paths.yaml"
            ),
            run_url=args.run_url,
            output=args.output,
            markdown=args.summary,
            github_output=args.github_output,
        )
        return 0


def _hosted_checks(args: argparse.Namespace) -> int:
    control.import_checkout(
        args.repository_path, args.base_checkout, args.base_sha, "base"
    )
    control.import_checkout(
        args.repository_path, args.head_checkout, args.head_sha, "head"
    )
    command = [
        "--control",
        str(args.control),
        "--repository-path",
        str(args.repository_path),
        "--base",
        args.base_sha,
        "--head",
        args.head_sha,
        "--output",
        str(args.output),
        "--markdown",
        str(args.summary),
    ]
    if args.github_output is not None:
        command.extend(("--github-output", str(args.github_output)))
    return hosted_checks.main(command)


def _report(args: argparse.Namespace) -> int:
    return report.main(
        [
            "--control",
            str(args.control),
            "--results",
            str(args.results),
            "--run-url",
            args.run_url,
            "--attempt-id",
            args.attempt_id,
            "--head-sha",
            args.head_sha,
            "--output",
            str(args.output),
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    _identity_arguments(prepare)
    prepare.add_argument("--contract-sha", required=True)
    prepare.add_argument("--attempt-id", required=True)
    prepare.add_argument("--repository", required=True)
    prepare.add_argument("--pr-number", type=int, required=True)
    prepare.add_argument("--run-url", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--jobs", type=Path, required=True)
    prepare.set_defaults(handler=_prepare)

    plan = commands.add_parser("plan")
    _identity_arguments(plan)
    plan.add_argument("--contract-sha", required=True)
    plan.add_argument("--repository", required=True)
    plan.add_argument("--pr-number", type=int, required=True)
    plan.add_argument("--run-url", required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--summary", type=Path, required=True)
    plan.add_argument("--github-output", type=Path)
    plan.set_defaults(handler=_plan)

    hosted = commands.add_parser("hosted-checks")
    _identity_arguments(hosted)
    hosted.add_argument("--control", type=Path, required=True)
    hosted.add_argument("--output", type=Path, required=True)
    hosted.add_argument("--summary", type=Path, required=True)
    hosted.add_argument("--github-output", type=Path)
    hosted.set_defaults(handler=_hosted_checks)

    reporter = commands.add_parser("report")
    reporter.add_argument("--control", type=Path, required=True)
    reporter.add_argument("--results", type=Path, required=True)
    reporter.add_argument("--run-url", required=True)
    reporter.add_argument("--attempt-id", required=True)
    reporter.add_argument("--head-sha", required=True)
    reporter.add_argument("--output", type=Path, required=True)
    reporter.set_defaults(handler=_report)

    args = parser.parse_args(argv)
    from ci import plugin

    factory = getattr(plugin, "repository_handlers", None)
    if factory is not None:
        handlers = factory()
        if (
            not isinstance(handlers, Mapping)
            or set(handlers) != {"prepare", "plan", "hosted-checks", "report"}
            or not all(callable(handler) for handler in handlers.values())
        ):
            raise ValueError("repository must register all command handlers")
        return handlers[args.command](args)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
