def planners(config_directory, repository, contributor_config_directory=None):
    return ()


def contributor_config_paths():
    return ("config/models.yaml", "config/scenarios.yaml")


def validate_job(job):
    allowed = {
        "id",
        "work_type",
        "component",
        "subject",
        "model",
        "profile",
        "changed_paths",
        "origins",
        "phases",
        "required_memory_gib",
        "required_disk_gib",
        "repository",
        "base_sha",
        "head_sha",
        "contract_sha",
        "manifest_digest",
        "synthetic",
        "hf_checkpoint",
        "scenarios",
        "unavailable_phases",
    }
    if unexpected := sorted(set(job) - allowed):
        raise ValueError(
            "work manifest contains unregistered fields: " + ", ".join(unexpected)
        )
    if (job.get("work_type"), job.get("component")) != (
        "ModelPath",
        "model_path",
    ):
        raise ValueError("unregistered work item")
    if any(phase not in {"synthetic", "hf_checkpoint"} for phase in job["phases"]):
        raise ValueError("work manifest contains unregistered phases")


def phase_commands(context):
    return {
        "synthetic": ["python", str(context.control / "ci" / "probe.py")],
        "hf_checkpoint": ["python", str(context.control / "ci" / "probe.py")],
    }


def validate_gate(gate):
    if gate.get("component") != "new_model_path":
        raise ValueError("unsupported gate")
    pending = gate.get("pending_work", {})
    if pending.get("model") != gate.get("model"):
        raise ValueError("approval gate pending work exceeds its scope")


def display_labels():
    return {
        "decode_tps": "Decode throughput",
        "embedding_latency_ms": "Embedding latency",
        "embedding_tps": "Embedding throughput",
        "hf_checkpoint": "HF checkpoint",
        "peak_memory_gib": "Peak memory",
        "prefill_tps": "Prefill throughput",
        "synthetic": "Synthetic",
        "ttft_ms": "TTFT",
        "wall_ms": "Wall time",
    }


def failure_messages():
    return {
        "checkpoint_not_found": "The configured checkpoint or revision was not found.",
        "access_denied": "The configured checkpoint requires access.",
        "disk_full": "The selected runner does not have enough disk space.",
        "network_transient": "The checkpoint download failed temporarily.",
    }
