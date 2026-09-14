# mlx-ci

Shared CI runtime for MLX repositories on Apple silicon.

## Responsibility

Participating repositories own change rules, checkpoint catalogs, fixtures,
resource estimates, and domain-specific probes. `mlx-ci` owns GitHub request
authorization, immutable planning, generic execution, result validation, and PR
reporting. `ci-runner` owns machine admission, checkpoint caching, sandboxing,
and cleanup.

The shared runtime must not contain model-family, modality, or product-specific
branches. A participant registers its behavior through `ci.plugin`; the central
runtime treats each resulting work item uniformly.

## Flow

1. A participant sends a planning request when a pull request changes and a run
   request after an authorized `/ci run` comment.
2. The private workflow revalidates the repository, request, permission, and
   current pull-request revisions.
3. Planning uses immutable base, head, and trusted contract checkouts. Only the
   trusted contract supplies executable CI code.
4. Independent work items are assigned to the smallest configured memory class
   that can contain them and queued as self-hosted GitHub Actions jobs.
5. The device broker checks current memory, disk, thermal, and local lease state,
   stages verified checkpoints, and runs the central executor in its sandbox.
6. The central reporter validates bounded structured results and posts a new
   comment for that immutable attempt.

GitHub Actions is the queue and attempt store. There is no second scheduler,
database, runner polling protocol, or signing service. Dynamic admission failures
are reported honestly; a later `/ci run` creates a new attempt.

## Participant interface

Each repository provides:

- `ci.plugin` registrations and validation hooks
- declarative configuration under `ci/config`
- revision-pinned checkpoints and fixtures
- probe and comparison code for its own domains
- a hash-pinned `ci/requirements.txt`

Shared code provides change matching, plan construction, hosted checks, manifest
sealing, phase ordering, isolated probe launch, result ingestion, and rendering.
The participant cannot replace the executor, findings destination, immutable
identity, or phase order.

## Deployment invariants

- Keep the central repository private and restrict its runner group to it.
- Scope the GitHub App to an explicit owner and repository allowlist.
- Never execute CI definitions from a pull-request head.
- Keep App credentials out of self-hosted jobs and sandbox environments.
- Pin actions and checkpoint revisions, disable checkout credential persistence,
  and validate every artifact and result boundary.
- Version shared contracts and validate changes against every participant and
  the runner before deployment.
