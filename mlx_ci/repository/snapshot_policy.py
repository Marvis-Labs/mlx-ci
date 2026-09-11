from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
)
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
SAFE_SUFFIXES = frozenset(
    {".json", ".model", ".safetensors", ".tiktoken", ".txt", ".yaml", ".yml"}
)
INERT_SUFFIXES = frozenset({".gitattributes", ".md"})
INERT_NAMES = frozenset({".gitattributes", "LICENSE", "NOTICE"})
UNSAFE_SUFFIXES = frozenset(
    {".bin", ".ckpt", ".dill", ".joblib", ".pickle", ".pkl", ".pt", ".pth", ".py"}
)


class CheckpointPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class CheckpointFile:
    path: str
    bytes: int
    sha256: str


def validate_checkpoint(checkpoint: Mapping[str, Any]) -> None:
    allowed = {
        "status",
        "repo",
        "revision",
        "expected_model_type",
        "weight",
        "files",
    }
    if set(checkpoint) != allowed or checkpoint.get("status") != "configured":
        raise CheckpointPolicyError("checkpoint contains unsupported fields")
    repo = checkpoint.get("repo")
    revision = checkpoint.get("revision")
    if not isinstance(repo, str) or REPOSITORY_PATTERN.fullmatch(repo) is None:
        raise CheckpointPolicyError("checkpoint repo must be an owner/name slug")
    if not isinstance(revision, str) or REVISION_PATTERN.fullmatch(revision) is None:
        raise CheckpointPolicyError("checkpoint revision must be a full commit SHA")
    model_type = checkpoint.get("expected_model_type")
    if model_type is not None and (not isinstance(model_type, str) or not model_type):
        raise CheckpointPolicyError("checkpoint expected_model_type is invalid")
    weight = checkpoint.get("weight")
    if not isinstance(weight, Mapping) or set(weight) != {"format", "files", "bytes"}:
        raise CheckpointPolicyError("checkpoint weight fields are invalid")
    if weight.get("format") != "safetensors":
        raise CheckpointPolicyError("checkpoint weight format must be safetensors")
    for field in ("files", "bytes"):
        value = weight.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise CheckpointPolicyError(f"checkpoint weight {field} must be positive")
    files = checkpoint.get("files")
    if not isinstance(files, list) or not files or len(files) > 64:
        raise CheckpointPolicyError("checkpoint files must be a bounded list")
    parsed = tuple(checkpoint_file(value) for value in files)
    names = [value.path for value in parsed]
    if names != sorted(set(names)):
        raise CheckpointPolicyError("checkpoint files must be sorted and unique")
    safetensors = [
        value for value in parsed if PurePosixPath(value.path).suffix == ".safetensors"
    ]
    if (
        len(safetensors) != weight["files"]
        or sum(value.bytes for value in safetensors) != weight["bytes"]
    ):
        raise CheckpointPolicyError("checkpoint weight summary does not match files")


def validate_snapshot(snapshot: Path, checkpoint: Mapping[str, Any]) -> dict[str, int]:
    normalized = dict(checkpoint)
    normalized.pop("id", None)
    normalized.pop("hf_url", None)
    normalized.pop("trust_remote_code", None)
    normalized["status"] = "configured"
    validate_checkpoint(normalized)
    root = snapshot.resolve(strict=True)
    if snapshot.is_symlink() or not root.is_dir():
        raise CheckpointPolicyError("checkpoint snapshot must be a real directory")
    actual = _tree_files(root)
    parsed = tuple(checkpoint_file(value) for value in normalized["files"])
    expected = {value.path for value in parsed}
    if actual != expected:
        raise CheckpointPolicyError("checkpoint files do not match allowlist")
    for expected_file in parsed:
        _verify_file(root, expected_file)
    try:
        config = json.loads((root / "config.json").read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CheckpointPolicyError("checkpoint config is invalid") from error
    _validate_config(config)
    if (
        normalized["expected_model_type"] is not None
        and config.get("model_type") != normalized["expected_model_type"]
    ):
        raise CheckpointPolicyError("checkpoint model_type does not match manifest")
    return {
        "files": len(parsed),
        "safetensors": int(normalized["weight"]["files"]),
        "weight_bytes": int(normalized["weight"]["bytes"]),
    }


def checkpoint_file(value: object) -> CheckpointFile:
    if not isinstance(value, Mapping) or set(value) != {"path", "bytes", "sha256"}:
        raise CheckpointPolicyError("invalid checkpoint file")
    path = value.get("path")
    if not isinstance(path, str):
        raise CheckpointPolicyError("invalid checkpoint file path")
    normalized = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or normalized.is_absolute()
        or ".." in normalized.parts
        or normalized.as_posix() != path
        or normalized.suffix not in SAFE_SUFFIXES
    ):
        raise CheckpointPolicyError(f"unsafe checkpoint file path: {path}")
    size = value.get("bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise CheckpointPolicyError("invalid checkpoint file bytes")
    digest = value.get("sha256")
    if not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None:
        raise CheckpointPolicyError("invalid checkpoint file sha256")
    return CheckpointFile(path, size, digest)


def _verify_file(directory: Path, expected: CheckpointFile) -> None:
    path = directory.joinpath(*PurePosixPath(expected.path).parts)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CheckpointPolicyError(
            f"cannot safely open checkpoint file: {expected.path}"
        ) from error
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != expected.bytes:
            raise CheckpointPolicyError(
                f"checkpoint file size does not match: {expected.path}"
            )
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1_048_576), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected.sha256:
            raise CheckpointPolicyError(
                f"checkpoint file digest does not match: {expected.path}"
            )
    finally:
        os.close(descriptor)


def _validate_config(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise CheckpointPolicyError("checkpoint config must be an object")
    stack = [value]
    while stack:
        current = stack.pop()
        for key, nested in current.items():
            if key in {"auto_map", "model_file"}:
                raise CheckpointPolicyError(
                    f"checkpoint config requires remote code: {key}"
                )
            if key == "trust_remote_code" and nested is True:
                raise CheckpointPolicyError("checkpoint config enables remote code")
            if isinstance(nested, Mapping):
                stack.append(nested)
            elif isinstance(nested, list):
                stack.extend(item for item in nested if isinstance(item, Mapping))


def _tree_files(directory: Path) -> set[str]:
    files: set[str] = set()
    stack = [(directory, PurePosixPath())]
    while stack:
        current, relative = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                nested = relative / entry.name
                if nested.as_posix() == ".marvis-ci-checkpoint.json":
                    continue
                if entry.is_symlink():
                    raise CheckpointPolicyError(
                        f"checkpoint tree contains a symlink: {nested}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), nested))
                elif entry.is_file(follow_symlinks=False):
                    files.add(nested.as_posix())
                else:
                    raise CheckpointPolicyError(
                        f"checkpoint tree contains an unsupported entry: {nested}"
                    )
    return files
