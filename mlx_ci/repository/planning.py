from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mlx_ci.repository.change_rules import ChangeContext, ChangeDetector, ChangeMatch
from mlx_ci.repository.components import ComponentContext


class ChangeComponent(Protocol):
    name: str

    def plan(
        self, matches: Sequence[ChangeMatch], context: ChangeContext
    ) -> dict[str, Any]: ...


class Delegator:
    def __init__(self, detector: ChangeDetector, components: Sequence[ChangeComponent]):
        self.detector = detector
        self.components = tuple(components)
        names = [component.name for component in self.components]
        if len(names) != len(set(names)):
            raise ValueError("component names must be unique")

    def plan(
        self,
        changed_files: Iterable[str],
        *,
        base_files: Iterable[str] = (),
        head_files: Iterable[str] = (),
        head_sha: str | None = None,
        base_sha: str | None = None,
        target_sha: str | None = None,
        tree_state_known: bool = False,
    ) -> dict[str, Any]:
        return self.plan_context(
            ChangeContext.create(
                changed_files,
                base_files,
                head_files,
                head_sha,
                base_sha,
                target_sha,
                tree_state_known,
            )
        )

    def plan_context(self, context: ChangeContext) -> dict[str, Any]:
        matches = self.detector.detect(context)
        grouped: dict[str, list[ChangeMatch]] = defaultdict(list)
        for match in matches:
            grouped[match.component].append(match)

        plans = []
        for component in self.components:
            component_matches = grouped.pop(component.name, [])
            if component_matches:
                plans.append(component.plan(component_matches, context))

        unregistered = [
            {
                "component": component,
                "rule": component_matches[0].rule,
                "changed_paths": sorted(match.path for match in component_matches),
                "reason": "unregistered_component",
            }
            for component, component_matches in sorted(grouped.items())
        ]
        return {
            "schema_version": 1,
            "base_sha": context.base_sha,
            "target_sha": context.target_sha,
            "head_sha": context.head_sha,
            "rules": list(dict.fromkeys(match.rule for match in matches)),
            "components": [plan["component"] for plan in plans],
            "jobs": [job for plan in plans for job in plan["jobs"]],
            "gates": [gate for plan in plans for gate in plan["gates"]],
            "checks": [check for plan in plans for check in plan.get("checks", [])],
            "blocked": [item for plan in plans for item in plan["blocked"]]
            + unregistered,
        }


def create_delegator(
    rules_config: Path,
    contributor_config_directory: Path | None = None,
    repository: Path | None = None,
) -> Delegator:
    from ci.plugin import planners

    config_directory = rules_config.parents[1]
    return Delegator(
        ChangeDetector.from_yaml(rules_config),
        planners(
            config_directory,
            repository or config_directory.parent,
            contributor_config_directory,
        ),
    )


def plan_changes(
    context: ChangeContext,
    rules_config: Path,
    contributor_config_directory: Path | None = None,
    repository: Path | None = None,
) -> dict[str, Any]:
    from ci import plugin

    repository_path = repository or rules_config.parents[2]
    factory = getattr(plugin, "plan_changes", None)
    if factory is None:
        return create_delegator(
            rules_config,
            contributor_config_directory,
            repository_path,
        ).plan_context(context)
    result = factory(
        context,
        ComponentContext(
            rules_config.parents[1],
            repository_path,
            contributor_config_directory,
        ),
    )
    if not isinstance(result, Mapping):
        raise ValueError("repository planner must return a plan mapping")
    return dict(result)


@dataclass(frozen=True)
class GitDiff:
    base_sha: str
    target_sha: str
    head_sha: str
    changed_files: tuple[str, ...]
    base_files: tuple[str, ...]
    head_files: tuple[str, ...]

    def context(self) -> ChangeContext:
        return ChangeContext.create(
            self.changed_files,
            self.base_files,
            self.head_files,
            head_sha=self.head_sha,
            base_sha=self.base_sha,
            target_sha=self.target_sha,
            tree_state_known=True,
        )


def _resolve_commit(ref: str, cwd: Path | None) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _merge_base(base: str, head: str, cwd: Path | None) -> str:
    result = subprocess.run(
        ["git", "merge-base", base, head],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _parse_name_status(output: bytes) -> tuple[str, ...]:
    fields = output.rstrip(b"\0").split(b"\0") if output else []
    changed = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii")
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(fields):
            raise ValueError("malformed git diff output")
        changed.extend(os.fsdecode(path) for path in fields[index : index + path_count])
        index += path_count
    return tuple(changed)


def _tree_files(commit: str, cwd: Path | None) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", commit, "--"],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    return tuple(
        os.fsdecode(path) for path in result.stdout.rstrip(b"\0").split(b"\0") if path
    )


def diff_from_git(base: str, head: str, cwd: Path | None = None) -> GitDiff:
    base_commit = _resolve_commit(base, cwd)
    head_commit = _resolve_commit(head, cwd)
    base_tree = _merge_base(base_commit, head_commit, cwd)
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--diff-filter=ACDMRTUXB",
            f"{base_tree}..{head_commit}",
            "--",
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    return GitDiff(
        base_sha=base_tree,
        target_sha=base_commit,
        head_sha=head_commit,
        changed_files=_parse_name_status(result.stdout),
        base_files=_tree_files(base_tree, cwd),
        head_files=_tree_files(head_commit, cwd),
    )


def changed_files_from_git(
    base: str, head: str, cwd: Path | None = None
) -> tuple[str, ...]:
    return diff_from_git(base, head, cwd).changed_files
