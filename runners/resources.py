from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

GIB = 1 << 30
MEMORY_TIERS_GIB = (16, 32, 64, 128, 256, 512)
MIN_DEVICE_RESERVE = 4 * GIB
MIN_AVAILABLE_RESERVE = 4 * GIB
MIN_MODEL_OVERHEAD = 2 * GIB
MIN_WORKSPACE = 4 * GIB
DOWNLOAD_RESERVE = 10 * GIB
CACHE_STATES = frozenset({"complete", "partial", "absent"})
FREE_PERCENT = re.compile(r"free percentage:\s*(\d+)%")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


class ResourceError(ValueError):
    pass


def estimate_peak_bytes(resources: dict[str, int]) -> int:
    resident = resources["resident_bytes"]
    overhead = max(MIN_MODEL_OVERHEAD, (resident + 19) // 20)
    variable = (
        resources["bytes_per_unit"] * resources["units"] * resources["batch_size"]
    )
    return resident + overhead + resources["fixed_bytes"] + variable


def device_reserve_bytes(physical_memory_bytes: int) -> int:
    return max(MIN_DEVICE_RESERVE, (physical_memory_bytes + 9) // 10)


def memory_tier_gib(estimated_peak_bytes: int) -> int:
    for tier in MEMORY_TIERS_GIB:
        physical = tier * GIB
        if estimated_peak_bytes <= physical - device_reserve_bytes(physical):
            return tier
    raise ResourceError("no runner memory tier can fit this job")


def disk_requirement_bytes(
    resources: dict[str, int], artifact: dict[str, Any] | None, cached: bool
) -> int:
    workspace = max(MIN_WORKSPACE, resources["workspace_bytes"])
    if artifact is None or cached:
        return workspace
    download = (artifact["tensor_bytes"] * 11 + 9) // 10
    return workspace + DOWNLOAD_RESERVE + download


def calculate_requirements(
    resources: dict[str, int], artifact: dict[str, Any] | None
) -> tuple[int, int, int]:
    peak = estimate_peak_bytes(resources)
    memory = memory_tier_gib(peak)
    disk = (disk_requirement_bytes(resources, artifact, False) + GIB - 1) // GIB
    return peak, memory, disk


def runner_decision(job: dict[str, Any], runner: dict[str, Any]) -> dict[str, Any]:
    validate_runner(runner)
    artifact = job["artifact"]
    cache_state = _cache_state(artifact, runner["artifacts"])
    cached = cache_state == "complete"
    peak = job["estimated_peak_bytes"]
    physical = runner["physical_memory_bytes"]
    available = runner["available_memory_bytes"]
    disk = disk_requirement_bytes(job["resources"], artifact, cached)
    reason = "eligible"
    if runner["busy"]:
        reason = "busy"
    elif peak > physical - device_reserve_bytes(physical):
        reason = "insufficient_memory"
    elif peak > max(0, available - MIN_AVAILABLE_RESERVE):
        reason = "insufficient_available_memory"
    elif disk > runner["free_disk_bytes"]:
        reason = "insufficient_disk"
    return {
        "eligible": reason == "eligible",
        "reason": reason,
        "cache_state": cache_state,
        "estimated_peak_bytes": peak,
        "required_disk_bytes": disk,
    }


def select_runner(job: dict[str, Any], runners: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = []
    for runner in runners:
        decision = runner_decision(job, runner)
        if decision["eligible"]:
            candidates.append(
                (
                    runner["physical_memory_bytes"],
                    decision["cache_state"] != "complete",
                    runner["id"],
                    runner,
                )
            )
    if not candidates:
        raise ResourceError("no eligible runner")
    return min(candidates, key=lambda item: item[:3])[-1]


def validate_runner(runner: Any) -> dict[str, Any]:
    fields = {
        "schema_version",
        "id",
        "physical_memory_bytes",
        "available_memory_bytes",
        "free_disk_bytes",
        "busy",
        "artifacts",
    }
    if not isinstance(runner, dict) or set(runner) != fields:
        raise ResourceError("runner fields are invalid")
    if runner["schema_version"] != 1 or type(runner["schema_version"]) is not int:
        raise ResourceError("runner version is invalid")
    if not isinstance(runner["id"], str) or NAME.fullmatch(runner["id"]) is None:
        raise ResourceError("runner id is invalid")
    for field, maximum in (
        ("physical_memory_bytes", 4 * (1 << 40)),
        ("available_memory_bytes", 4 * (1 << 40)),
        ("free_disk_bytes", 16 * (1 << 40)),
    ):
        value = runner[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > maximum
        ):
            raise ResourceError(f"runner {field} is invalid")
    if runner["available_memory_bytes"] > runner["physical_memory_bytes"]:
        raise ResourceError("runner available memory is invalid")
    if type(runner["busy"]) is not bool:
        raise ResourceError("runner busy state is invalid")
    artifacts = runner["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > 128:
        raise ResourceError("runner artifacts are invalid")
    identities = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {
            "kind",
            "repository",
            "revision",
            "state",
        }:
            raise ResourceError("runner artifact fields are invalid")
        if artifact["kind"] != "huggingface":
            raise ResourceError("runner artifact kind is invalid")
        if (
            not isinstance(artifact["repository"], str)
            or REPOSITORY.fullmatch(artifact["repository"]) is None
        ):
            raise ResourceError("runner artifact repository is invalid")
        if (
            not isinstance(artifact["revision"], str)
            or SHA.fullmatch(artifact["revision"]) is None
        ):
            raise ResourceError("runner artifact revision is invalid")
        if artifact["state"] not in CACHE_STATES:
            raise ResourceError("runner artifact state is invalid")
        identity = (
            artifact["kind"],
            artifact["repository"],
            artifact["revision"],
        )
        if identity in identities:
            raise ResourceError("runner artifact is duplicated")
        identities.add(identity)
    return runner


def probe_runner(
    runner_id: str,
    artifact: dict[str, Any] | None,
    cache_directory: Path,
    busy: bool,
) -> dict[str, Any]:
    physical = int(
        subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.memsize"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    pressure = subprocess.run(
        ["/usr/bin/memory_pressure", "-Q"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    match = FREE_PERCENT.search(pressure)
    if match is None:
        raise ResourceError("available memory could not be measured")
    artifacts = []
    if artifact is not None:
        artifacts.append(
            {
                "kind": artifact["kind"],
                "repository": artifact["repository"],
                "revision": artifact["revision"],
                "state": huggingface_cache_state(artifact, cache_directory),
            }
        )
    snapshot = {
        "schema_version": 1,
        "id": runner_id,
        "physical_memory_bytes": physical,
        "available_memory_bytes": physical * int(match.group(1)) // 100,
        "free_disk_bytes": shutil.disk_usage(cache_directory).free,
        "busy": busy,
        "artifacts": artifacts,
    }
    return validate_runner(snapshot)


def huggingface_cache_state(artifact: dict[str, Any], cache_directory: Path) -> str:
    repository = artifact["repository"].replace("/", "--")
    snapshot = (
        cache_directory / f"models--{repository}" / "snapshots" / artifact["revision"]
    )
    if not snapshot.is_dir():
        return "absent"
    try:
        if not (snapshot / "config.json").is_file():
            return "partial"
        index = snapshot / "model.safetensors.index.json"
        if index.is_file():
            if index.stat().st_size > 64 * (1 << 20):
                return "partial"
            data = json.loads(index.read_text(encoding="utf-8"))
            weight_map = data.get("weight_map")
            if not isinstance(weight_map, dict) or len(weight_map) > 1_000_000:
                return "partial"
            names = set(weight_map.values())
            if (
                not names
                or len(names) > 4_096
                or any(
                    not isinstance(name, str)
                    or Path(name).name != name
                    or not name.endswith(".safetensors")
                    for name in names
                )
            ):
                return "partial"
            weights = [snapshot / name for name in names]
        else:
            weights = list(snapshot.glob("*.safetensors"))
        if not weights or any(not path.is_file() for path in weights):
            return "partial"
        present = sum(path.stat().st_size for path in weights)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "partial"
    if present < artifact["tensor_bytes"]:
        return "partial"
    return "complete"


def _cache_state(
    artifact: dict[str, Any] | None, cached_artifacts: list[dict[str, Any]]
) -> str:
    if artifact is None:
        return "complete"
    for cached in cached_artifacts:
        if all(
            cached[field] == artifact[field]
            for field in ("kind", "repository", "revision")
        ):
            return cached["state"]
    return "absent"
