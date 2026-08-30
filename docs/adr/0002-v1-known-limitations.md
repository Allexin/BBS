# ADR 0002: accepted v1 limitations

Status: accepted

Date: 2026-08-31

## Context

Stage 10 requires every known limitation to be fixed or explicitly accepted. The
following constraints are deliberate boundaries of v1, not unfinished behavior.

## Decision

- Operations execute serially. BBS does not run backup jobs in parallel. This keeps
  disk, VSS, queue, and cancellation ownership unambiguous; additional jobs wait in
  the durable queue.
- BBS is Windows-specific and privileged disk/VSS work runs through the LocalSystem
  executor. Service control and initial ACL installation require elevation; ordinary
  configuration editing in the approved local deployment does not.
- The Web UI is static and read-only. It has no remote command API, login system, or
  embedded HTTP server. Operational commands remain local through `backupctl`.
- File restore covers content and logical paths, not a bare-metal Windows image.
  ACLs, audit rules, alternate data streams, hardlink identity, sparse allocation,
  compression/encryption flags, and other NTFS-specific metadata are outside the v1
  restore contract. Workloads requiring those properties need a separate system-image
  recovery mechanism.
- Restic compatibility is pinned to the accepted version. Updating restic requires
  renewed compatibility and fault-classification tests. Password-protected repository
  recovery depends on an independent copy of the job config/passphrase.
- Scheduler intentionally performs no catch-up after downtime. A lost manager database
  starts new history and requires a full recovery check; old UI/run history is not
  reconstructed from the repository.
- Automatic scheduled restore tests are not part of v1. Restore tests are explicit
  local operator actions; Stage 10 proves the restore path before production jobs are
  created.
- SMART data is advisory hardware telemetry, not a guarantee or failure prediction.
  Unsupported attributes/tests remain visible as warning/unknown, and active workload
  may delay a self-test. Critical data must not depend on SMART alone.
- The current Dev-to-Stable updater is a local development tool with no automatic
  rollback. It never replaces Stable data/config, and a failed switch remains visible
  for manual correction.

## Consequences

These limitations are acceptable for the v1 backup role and are documented in the
configuration, operations, and disaster-recovery manuals where operator action is
required. Expanding any boundary above is a separately designed post-v1 change rather
than an implicit compatibility promise.
