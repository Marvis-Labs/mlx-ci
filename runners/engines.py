from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from runners.contract import ContractError, NAME, REPOSITORY


OFFICIAL = re.compile(
    r"https://github\.com/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z"
)


def load_engine(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_096:
        raise ContractError("engine registration is invalid")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise ContractError("engine registration is invalid") from error
    if not isinstance(data, dict) or set(data) != {
        "official_repository",
        "repository",
        "maintainers",
    }:
        raise ContractError("engine registration fields are invalid")
    official = data["official_repository"]
    repository = data["repository"]
    maintainers = data["maintainers"]
    if not isinstance(official, str) or OFFICIAL.fullmatch(official) is None:
        raise ContractError("official repository is invalid")
    if not isinstance(repository, str) or REPOSITORY.fullmatch(repository) is None:
        raise ContractError("repository is invalid")
    if (
        not isinstance(maintainers, list)
        or not 1 <= len(maintainers) <= 16
        or any(
            not isinstance(item, str) or NAME.fullmatch(item) is None
            for item in maintainers
        )
        or len({item.lower() for item in maintainers}) != len(maintainers)
    ):
        raise ContractError("maintainers are invalid")
    return data


def load_engines(directory: Path) -> dict[str, dict[str, Any]]:
    engines = {}
    for path in sorted(directory.glob("*.yml")):
        if NAME.fullmatch(path.stem) is None:
            raise ContractError("engine name is invalid")
        engines[path.stem] = load_engine(path)
    if not engines:
        raise ContractError("no engines are registered")
    repositories = [engine["repository"] for engine in engines.values()]
    if len(repositories) != len(set(repositories)):
        raise ContractError("repository is registered twice")
    return engines
