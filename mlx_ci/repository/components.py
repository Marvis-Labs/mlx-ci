from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ComponentContext:
    config_directory: Path
    repository: Path
    contributor_config_directory: Path | None = None

    def config(self, relative_path: str, *, contributor: bool = False) -> Path:
        if contributor and self.contributor_config_directory is not None:
            candidate = self.contributor_config_directory / relative_path
            if candidate.is_file():
                return candidate
        return self.config_directory / relative_path

@dataclass(frozen=True)
class ExecutionContext:
    job_path: Path
    control: Path
    base: Path
    head: Path

    @property
    def config_directory(self) -> Path:
        return self.control / "ci"


PhaseCommand = Callable[[ExecutionContext], list[str]]
GateValidator = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class PhaseRegistration:
    name: str
    command: PhaseCommand


@dataclass(frozen=True)
class ComponentRegistration:
    name: str
    components: frozenset[str]
    planner_factory: Callable[[ComponentContext], tuple[Any, ...]]
    work: frozenset[tuple[str, str]]
    phases: tuple[PhaseRegistration, ...] = ()
    contributor_configs: frozenset[str] = frozenset()
    gate_validator: GateValidator | None = None
    job_fields: frozenset[str] = frozenset()
