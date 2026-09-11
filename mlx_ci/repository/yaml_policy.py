from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml

MAX_YAML_BYTES = 1_048_576
ErrorType = TypeVar("ErrorType", bound=ValueError)


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _UniqueSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    value: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in value:
            raise ValueError(f"duplicate YAML key: {key}")
        value[key] = loader.construct_object(value_node, deep=deep)
    return value


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def load_yaml_mapping(
    path: Path, error_type: type[ErrorType], label: str
) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise error_type(f"{path}: {label} is not a real file")
    if path.stat().st_size > MAX_YAML_BYTES:
        raise error_type(f"{path}: {label} exceeds size limit")
    try:
        value = yaml.load(path.read_text(), Loader=_UniqueSafeLoader)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as error:
        raise error_type(f"{path}: invalid YAML") from error
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise error_type(f"{path}: {label} must be a mapping")
    return value
