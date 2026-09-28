from __future__ import annotations

from typing import Any

GIB = 1 << 30
MEMORY_TIERS_GIB = (16, 32, 64, 128, 256, 512)
MIN_DEVICE_RESERVE = 4 * GIB
MIN_MODEL_OVERHEAD = 2 * GIB
MIN_WORKSPACE = 4 * GIB
DOWNLOAD_RESERVE = 10 * GIB


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


def calculate_requirements(
    resources: dict[str, int], artifact: dict[str, Any] | None
) -> tuple[int, int, int]:
    peak = estimate_peak_bytes(resources)
    memory = memory_tier_gib(peak)
    disk_bytes = max(MIN_WORKSPACE, resources["workspace_bytes"])
    if artifact is not None:
        disk_bytes += DOWNLOAD_RESERVE + (artifact["tensor_bytes"] * 11 + 9) // 10
    disk = (disk_bytes + GIB - 1) // GIB
    return peak, memory, disk
