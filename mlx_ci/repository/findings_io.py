from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate result field")
        value[key] = item
    return value


def load_record(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("result must be a real file")
    with path.open("rb") as stream:
        payload = stream.read(1_048_577)
    if len(payload) > 1_048_576:
        raise ValueError("result exceeds size limit")
    value = json.loads(payload, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("result must contain an object")
    return value


def validate_findings_path(path: Path, *, protected: Sequence[Path]) -> Path:
    if not path.name or path.name in {".", ".."}:
        raise ValueError("invalid findings path")
    target = path.parent.resolve(strict=True) / path.name
    for root in protected:
        if target.is_relative_to(root.resolve(strict=True)):
            raise ValueError("findings path overlaps protected input")
    if target.exists() or target.is_symlink():
        raise FileExistsError("findings path already exists")
    return target


def write_findings(path: Path, result: Mapping, *, protected: Sequence[Path]) -> None:
    """Create a bounded result without overwriting inputs or an existing result."""
    target = validate_findings_path(path, protected=protected)
    payload = (
        json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    if len(payload) > 1_048_576:
        raise ValueError("findings exceed the result budget")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
