import pytest

from mlx_ci.repository.yaml_policy import MAX_YAML_BYTES, load_yaml_mapping


@pytest.mark.parametrize("payload", ["x: 1\nx: 2\n", "[1, 2]", "1: value"])
def test_yaml_requires_unique_string_mapping_keys(tmp_path, payload):
    path = tmp_path / "policy.yaml"
    path.write_text(payload)
    with pytest.raises(ValueError):
        load_yaml_mapping(path, ValueError, "policy")


def test_yaml_rejects_symlink_and_oversized_files(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("x: 1\n")
    assert load_yaml_mapping(path, ValueError, "policy") == {"x": 1}
    alias = tmp_path / "alias.yaml"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="real file"):
        load_yaml_mapping(alias, ValueError, "policy")
    path.write_text("x" * (MAX_YAML_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        load_yaml_mapping(path, ValueError, "policy")
