# mlx-ci

Private trusted control plane for Marvis-Labs Apple-silicon CI.

`mlx-ci` coordinates CI for registered repositories. `mlx-vlm` and `mlx-audio`
are its first consumers, but the control plane does not know about models,
modalities, or repository-specific work types. Keep the boundaries below strict.

| Repository | Owns |
| --- | --- |
| Participating repositories | Change rules, domain catalogs, fixtures, planners, probes, executors, correctness policy, resource estimates, result validation, and bot rendering |
| `mlx-ci` | GitHub App authorization, trusted orchestration, global queueing, runner inventory, smallest-fit selection, cross-repository leases, immutable attempts, retry and escalation, manifest signing, result transport, and PR status delivery |
| `ci-runner` | Machine setup, runner registration, capability and heartbeat reporting, local atomic leases, checkpoint caching, asset staging, sandboxing, cleanup, and execution of sealed work manifests |

## Control flow

1. The GitHub App receives an exact `/ci run` comment from an allowlisted
   repository and verifies the commenter has write or maintain permission.
2. The control plane resolves immutable base, head, and trusted contract SHAs.
   Repository CI code always comes from the configured trusted CI ref, never
   from the pull-request head.
3. The trusted repository planner emits independent work items with required
   memory, disk, phases, fixtures, and revision-pinned checkpoints.
4. The global scheduler selects the smallest live runner that satisfies the
   work item, acquires a cross-repository lease, and escalates only when the
   smaller device is occupied, unavailable, or rejects the workload.
5. `mlx-ci` signs the canonical work manifest. The selected runner verifies the
   signature and immutable SHAs before entering its restricted sandbox.
6. The runner executes static and synthetic checks before real checkpoints.
   Performance runs only after correctness passes.
7. `mlx-ci` validates the structured result and publishes the repository-owned
   rendering to the originating pull request.

## Repository interface

Participating repositories expose three trusted modules under `ci/`:
`ci.control` creates sealed flat runner manifests, `ci.work_executor` runs the
registered phases, and `ci.report` renders validated results. They also provide
`ci/hosted-requirements.txt` with hash-pinned dependencies for planning and
reporting. The reusable workflow in this repository owns authorization,
immutable checkouts, generic queue preparation, runner dispatch, artifacts, and
comment delivery.

The control plane wraps each repository manifest without interpreting its
payload. Before dispatch it verifies that work identity, repository, revisions,
phases, and resource requirements agree across both layers, then restores the
original flat manifest expected by the generic runner.

## Security invariants

- Keep this repository private. The self-hosted runner group must allow only
  this repository and an exact workflow on its protected default branch.
- Use a least-privilege GitHub App with an explicit repository allowlist. Do
  not use a long-lived organization PAT in workflows or on runners.
- Never execute pull-request workflow code, planners, actions, shell fragments,
  or bot renderers outside the restricted runner sandbox.
- Treat pull-request metadata, source trees, artifacts, cache entries, model
  output, and runner output as untrusted data. Parse and validate every boundary.
- Seal each job with immutable repository, base, head, contract, and attempt
  identifiers plus a canonical digest and control-plane signature.
- Keep checkpoint repositories revision-pinned. Validate exact file paths,
  sizes, and SHA-256 hashes before publishing or reusing a cache entry.
- Keep leases global across repositories and atomic. A local runner lease is a
  second line of defense, not the scheduler's source of truth.
- Never copy raw runner errors into comments. Emit only validated result and
  failure classes.
- Preserve evidence order: static, synthetic, real model, then correctness-gated
  performance. Report each evidence class honestly and independently.

## Agent guidance

Before changing any participating repository, read this file and preserve these
ownership boundaries. Shared orchestration belongs here only when it is neutral
to model type and repository policy. Model-family knowledge stays in the model
repository; device-specific behavior stays in `ci-runner`.

Changes to a shared contract must be versioned, backward-compatible during
migration, and tested against `mlx-ci`, `ci-runner`, `mlx-vlm`, and `mlx-audio`.
Use isolated worktrees, preserve unrelated work, make signed commits, and do not
enable a workflow or runner group until its trusted path and rollback have been
verified.
