# mlx-ci

Private trusted control plane for Marvis-Labs Apple-silicon CI.

`mlx-ci` coordinates CI for registered repositories. `mlx-vlm` and `mlx-audio`
are its first consumers, but the control plane does not know about models,
modalities, or repository-specific work types. Keep the boundaries below strict.

| Repository | Owns |
| --- | --- |
| Participating repositories | Declarative change rules, domain catalogs, fixtures, model-specific planners and probes, resource estimates, and a small `ci.plugin` registration surface |
| `mlx-ci` | Change detection, plan assembly, hosted checks, execution, result validation, bot rendering, GitHub App authorization, global queueing, runner inventory, smallest-fit selection, leases, immutable attempts, retry and escalation, signing, and delivery |
| `ci-runner` | Machine setup, runner registration, capability and heartbeat reporting, local atomic leases, checkpoint caching, asset staging, sandboxing, cleanup, and execution of sealed work manifests |

## Control flow

1. The GitHub App receives an exact `/ci run` comment from an allowlisted
   repository and verifies the commenter has write or maintain permission.
2. The control plane resolves immutable base, head, and trusted contract SHAs.
   Repository CI code always comes from the configured trusted CI ref, never
   from the pull-request head.
3. The shared planner loads the repository plugin and emits independent work items with required
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

Participating repositories expose `ci.plugin`, declarative configuration under
`ci/config`, model-specific probes, assets, and hash-pinned `ci/requirements.txt`.
The shared repository runtime provides planning, hosted checks, execution,
validation, and reporting commands. The private workflows own authorization,
immutable checkouts, queue preparation, runner dispatch, artifacts, and comment
delivery.

The control plane imports only the participant's narrow plugin contract. Model
knowledge and inference stay in the model repository; lifecycle and safety
machinery stay shared so audio and vision-language CI do not fork it.
The plugin supplies planners, contributor configuration paths, job and gate
validation, and phase commands; labels and contributor-facing failure messages
are optional. The participant owns its work types, phases, and payload fields.

The control plane wraps each repository manifest without interpreting its
payload. Before dispatch it verifies that work identity, repository, revisions,
phases, and resource requirements agree across both layers, then restores the
original flat manifest expected by the generic runner.

Each accepted `/ci run` delivery creates a separate immutable attempt. Replaying
the same delivery is idempotent, but a later command for the same pull request
and head revision does not coalesce with earlier work.

Runner transport is a bounded JSON WSGI interface intended to sit behind a TLS
reverse proxy. Every device has an independent high-entropy bearer token, while
trusted ingestion uses a separate credential. The service stores token digests,
assigns heartbeat timestamps, binds renewals, responses, and results to both the
runner identity and lease generation, and returns only Ed25519-signed canonical
manifests. Private signing keys and credential files must be service-owned,
non-symlinked, and inaccessible to group or other users. Runners trust an
explicit key-ID-to-public-key map so rotations can overlap without accepting an
unknown signer.

The application factory reads `MLX_CI_STATE_PATH`,
`MLX_CI_RUNNER_CREDENTIALS`, `MLX_CI_SIGNING_KEY`,
`MLX_CI_SIGNING_KEY_ID`, and `MLX_CI_QUEUE_TOKEN_DIGEST`. It intentionally does
not provide a cleartext development server.

The GitHub Actions dispatch path is a migration bridge until result-triggered
reporting, runner-side signature polling, and a durable service deployment are
configured. Participant repositories forward only repository, pull-request, and
comment identifiers. The private workflow re-fetches and validates the comment,
maintainer permission, and immutable revisions through the GitHub App before
preparing work. App credentials never enter a self-hosted runner job.

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

Repository plugins provide `validate_job` and `phase_commands`; the shared executor
owns sealing, clean-checkout verification, process launch, phase ordering, and
bounded findings. Optional `validate_phase`, `protected_inputs`, and
`phase_environment` hooks carry repository-specific checks and inputs. Repository
settings cannot replace the controller's Python path or findings destination.
There are no model-type execution branches or image/generation defaults here.
Command handlers may be supplied through `repository_handlers` without another
CLI parser. Model-specific planning and reporting remain participant-owned.

Before changing any participating repository, read this file and preserve these
ownership boundaries. Shared orchestration belongs here only when it is neutral
to model type and repository policy. Model-family knowledge stays in the model
repository; device-specific behavior stays in `ci-runner`.

Changes to a shared contract must be versioned, backward-compatible during
migration, and tested against `mlx-ci`, `ci-runner`, `mlx-vlm`, and `mlx-audio`.
Use isolated worktrees, preserve unrelated work, make signed commits, and do not
enable a workflow or runner group until its trusted path and rollback have been
verified.
