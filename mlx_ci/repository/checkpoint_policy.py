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
    {
        ".gitattributes",
        ".jpeg",
        ".jpg",
        ".json",
        ".md",
        ".model",
        ".png",
        ".safetensors",
        ".tiktoken",
        ".txt",
        ".yaml",
        ".yml",
    }
)
SAFE_NAMES = frozenset({".gitattributes", "LICENSE", "NOTICE"})
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
    exact = "files" in checkpoint or "status" in checkpoint
    expected = (
        {
            "status",
            "repo",
            "revision",
            "expected_model_type",
            "weight",
            "files",
        }
        if exact
        else {"repo", "revision", "expected_model_type", "weight"}
    )
    if set(checkpoint) != expected or (
        exact and checkpoint.get("status") != "configured"
    ):
        raise CheckpointPolicyError("checkpoint contains unsupported fields")
    repo = checkpoint.get("repo")
    revision = checkpoint.get("revision")
    if not isinstance(repo, str) or REPOSITORY_PATTERN.fullmatch(repo) is None:
        raise CheckpointPolicyError("checkpoint repo must be an owner/name slug")
    if not isinstance(revision, str) or REVISION_PATTERN.fullmatch(revision) is None:
        raise CheckpointPolicyError("checkpoint revision must be a full commit SHA")
    model_type = checkpoint.get("expected_model_type")
    if exact:
        if model_type is not None and (
            not isinstance(model_type, str) or not model_type
        ):
            raise CheckpointPolicyError("checkpoint expected_model_type is invalid")
    elif not isinstance(model_type, str) or not model_type:
        raise CheckpointPolicyError("checkpoint expected_model_type is required")
    weight = checkpoint.get("weight")
    if not isinstance(weight, Mapping):
        raise CheckpointPolicyError("checkpoint weight fields are invalid")
    required_weight_fields = {"format", "files", "bytes"}
    allowed_weight_fields = required_weight_fields | {"gib"}
    if set(weight) - allowed_weight_fields or "bytes" not in weight:
        raise CheckpointPolicyError("checkpoint weight fields are invalid")
    if weight.get("format", "safetensors") != "safetensors":
        raise CheckpointPolicyError("checkpoint weight format must be safetensors")
    for field in ("bytes", "files") if "files" in weight else ("bytes",):
        value = weight.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise CheckpointPolicyError(f"checkpoint weight {field} must be positive")
    if not exact:
        return
    if not required_weight_fields.issubset(weight):
        raise CheckpointPolicyError("checkpoint weight fields are invalid")
    files = checkpoint.get("files")
    if not isinstance(files, list) or not files or len(files) > 128:
        raise CheckpointPolicyError("checkpoint files must be a bounded list")
    parsed = tuple(checkpoint_file(value) for value in files)
    names = [value.path for value in parsed]
    if names != sorted(set(names)):
        raise CheckpointPolicyError("checkpoint files must be sorted and unique")
    weights = [
        value for value in parsed if PurePosixPath(value.path).suffix == ".safetensors"
    ]
    if (
        len(weights) != weight["files"]
        or sum(value.bytes for value in weights) != weight["bytes"]
    ):
        raise CheckpointPolicyError("checkpoint weight summary does not match files")


def validate_snapshot(snapshot: Path, checkpoint: Mapping[str, Any]) -> dict[str, int]:
    normalized = dict(checkpoint)
    normalized.pop("id", None)
    normalized.pop("hf_url", None)
    normalized.pop("trust_remote_code", None)
    exact = "files" in normalized
    if exact:
        normalized["status"] = "configured"
    else:
        normalized.pop("status", None)
    validate_checkpoint(normalized)
    root = snapshot.resolve(strict=True)
    if snapshot.is_symlink() or not root.is_dir():
        raise CheckpointPolicyError("checkpoint snapshot must be a real directory")
    if exact:
        return _validate_exact_snapshot(root, normalized)
    return _validate_bounded_snapshot(root, normalized)


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
        or normalized.suffix.lower() not in SAFE_SUFFIXES
    ):
        raise CheckpointPolicyError(f"unsafe checkpoint file path: {path}")
    size = value.get("bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise CheckpointPolicyError("invalid checkpoint file bytes")
    digest = value.get("sha256")
    if not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None:
        raise CheckpointPolicyError("invalid checkpoint file sha256")
    return CheckpointFile(path, size, digest)


def _validate_exact_snapshot(
    root: Path, checkpoint: Mapping[str, Any]
) -> dict[str, int]:
    parsed = tuple(checkpoint_file(value) for value in checkpoint["files"])
    if _tree_files(root) != {value.path for value in parsed}:
        raise CheckpointPolicyError("checkpoint files do not match allowlist")
    for expected in parsed:
        _verify_file(root, expected)
    _validate_model_config(root, checkpoint)
    return {
        "files": len(parsed),
        "safetensors": int(checkpoint["weight"]["files"]),
        "weight_bytes": int(checkpoint["weight"]["bytes"]),
    }


def _validate_bounded_snapshot(
    root: Path, checkpoint: Mapping[str, Any]
) -> dict[str, int]:
    entries = sorted(root.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise CheckpointPolicyError("checkpoint contains a symlink")
    files = [path for path in entries if path.is_file()]
    if not files or len(files) > 10_000:
        raise CheckpointPolicyError("checkpoint snapshot has an invalid file count")
    total = 0
    weight_files = 0
    for path in files:
        relative = path.resolve(strict=True).relative_to(root)
        suffix = relative.suffix.lower()
        if suffix in UNSAFE_SUFFIXES or (
            relative.name not in SAFE_NAMES and suffix not in SAFE_SUFFIXES
        ):
            raise CheckpointPolicyError(f"unsafe checkpoint file: {relative}")
        size = path.stat().st_size
        if size > int(checkpoint["weight"]["bytes"]) + 512 * 2**20:
            raise CheckpointPolicyError(
                f"checkpoint file exceeds size policy: {relative}"
            )
        total += size
        weight_files += int(suffix == ".safetensors")
    limit = int(checkpoint["weight"]["bytes"] * 1.25) + 512 * 2**20
    expected_files = checkpoint["weight"].get("files")
    if total > limit or (expected_files is not None and weight_files != expected_files):
        raise CheckpointPolicyError("checkpoint snapshot exceeds declared policy")
    _validate_model_config(root, checkpoint)
    return {
        "files": len(files),
        "safetensors": weight_files,
        "weight_bytes": int(checkpoint["weight"]["bytes"]),
    }


def _validate_model_config(root: Path, checkpoint: Mapping[str, Any]) -> None:
    try:
        config = json.loads((root / "config.json").read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CheckpointPolicyError("checkpoint config is invalid") from error
    _validate_config(config)
    expected = checkpoint.get("expected_model_type")
    if expected is not None and config.get("model_type") != expected:
        raise CheckpointPolicyError("checkpoint model_type does not match manifest")


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
