import hashlib
import json
from types import SimpleNamespace

import pytest

from mlx_ci.repository.snapshot_policy import (
    CheckpointPolicyError,
    validate_checkpoint,
    validate_snapshot,
)


def checkpoint(config, weights):
    policy = {
        "repo": "example/test-checkpoint",
        "revision": "a" * 40,
        "expected_model_type": "example",
        "weight": {"format": "safetensors", "files": 1, "bytes": len(weights)},
        "files": [
            {
                "path": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, data in (("config.json", config), ("model.safetensors", weights))
        ],
    }
    return SimpleNamespace(
        verify_directory=lambda path: validate_snapshot(path, policy)
    )


def configured_checkpoint() -> dict[str, object]:
    return {
        "status": "configured",
        "repo": "example/test-checkpoint",
        "revision": "a" * 40,
        "expected_model_type": "example",
        "weight": {"format": "safetensors", "files": 1, "bytes": 7},
        "files": [
            {"path": "config.json", "bytes": 2, "sha256": "0" * 64},
            {"path": "model.safetensors", "bytes": 7, "sha256": "0" * 64},
        ],
    }


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("revision", "main", "full commit SHA"),
        ("repo", "invalid", "owner/name slug"),
        ("weight", {"format": "pytorch", "files": 1, "bytes": 7}, "safetensors"),
    ],
)
def test_mutable_or_unsafe_checkpoint_is_rejected(field, value, error):
    configured = configured_checkpoint()
    configured[field] = value

    with pytest.raises(CheckpointPolicyError, match=error):
        validate_checkpoint(configured)


def test_snapshot_requires_exact_allowlisted_files(tmp_path):
    config = json.dumps({"model_type": "example"}).encode()
    weights = b"weights"
    policy = checkpoint(config, weights)
    (tmp_path / "config.json").write_bytes(config)
    (tmp_path / "model.safetensors").write_bytes(weights)
    (tmp_path / ".marvis-ci-checkpoint.json").write_text("{}")
    policy.verify_directory(tmp_path)

    (tmp_path / "model.safetensors").write_bytes(b"changed")
    with pytest.raises(CheckpointPolicyError, match="digest does not match"):
        policy.verify_directory(tmp_path)

    (tmp_path / "extra.json").write_text("{}")
    with pytest.raises(CheckpointPolicyError, match="allowlist"):
        policy.verify_directory(tmp_path)


def test_snapshot_rejects_symlinks(tmp_path):
    config = json.dumps({"model_type": "example"}).encode()
    weights = b"weights"
    policy = checkpoint(config, weights)
    (tmp_path / "config.json").write_bytes(config)
    (tmp_path / "model.safetensors").write_bytes(weights)
    (tmp_path / "link.json").symlink_to(tmp_path / "config.json")

    with pytest.raises(CheckpointPolicyError, match="contains a symlink"):
        policy.verify_directory(tmp_path)
