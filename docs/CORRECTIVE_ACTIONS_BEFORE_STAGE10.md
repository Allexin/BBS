# Corrective actions before Stage 10

## Purpose

This document records the findings of the pre-Stage-10 project review and defines
the work required before end-to-end v1 acceptance. It supplements the normative
design and implementation-stage documents; it does not replace or restate them.

Stage 10 must not begin until every blocking item below has an automated test and
its acceptance evidence is recorded.

## Review baseline

- Reviewed revision: `331b938` (`main`).
- Automated baseline: 313 tests passed, 8 hardware/integration tests skipped.
- Ruff and strict mypy checks passed.
- The review covered production composition in addition to isolated components.

Passing component tests did not establish that the installed manager service could
perform a backup. The principal finding is a missing production integration layer.

## CA-01 — Compose the real manager runtime

**Severity:** blocking

`run_service` currently initializes the data layout and database, reconciles stale
runs, and waits for a stop signal. Scheduler, command processing, executor launch,
event ingestion, notifications, deadlines, reports, monitoring, and public
projection are implemented as separate components but are not instantiated by the
service.

This is important because NSSM can report a healthy running service while no
scheduled or manual backup can execute. Service-process acceptance alone therefore
produces a false readiness signal.

Required correction:

- introduce one production composition root for manager-owned components;
- run bounded periodic work without overlapping manager iterations;
- accept spool commands and poll enabled schedules;
- claim at most one operation and execute it in a child executor;
- ingest and durably apply executor events;
- dispatch notifications and publish sanitized status/health projections;
- keep auxiliary failures isolated from backup execution where the design requires;
- reconcile startup state before accepting new work.

Acceptance evidence:

- a composition test drives a manual command from `incoming` through a claimed run
  to a terminal database result using a fake executor transport;
- a scheduled trigger follows the same queue and execution path;
- projection generation observes the resulting durable state;
- an auxiliary transport failure does not terminate the service loop.

## CA-02 — Connect cooperative service shutdown to live work

**Severity:** blocking

The current shutdown callbacks are placeholders. No live command/schedule gate is
closed, no executor receives cancellation, and no final status is published.

This is important because backup-disk cleanup and confirmed offline state depend on
the executor being allowed to finish its `finally` path. A service that merely exits
cleanly is not equivalent to a cooperatively stopped backup service.

Required correction:

- stop accepting spool and schedule work first;
- discard only the queued tail according to the documented state transition;
- send cooperative cancellation to the active executor;
- wait without a hard timeout for executor cleanup;
- publish a final `stopping` projection after executor completion;
- close the database only after all runtime tasks have stopped.

Acceptance evidence:

- an integration test verifies the exact shutdown ordering with a live blocked fake
  executor;
- Stage 9 NSSM acceptance is repeated against the real manager composition.

## CA-03 — Enforce Stable and data ACL policy

**Severity:** high

Deployment and layout creation currently rely on inherited filesystem permissions.
They do not apply or verify the ACL policy required for Stable, protected configs,
the command spool, state, and local logs.

This is important because job configuration may contain a repository passphrase and
the spool is a privileged command boundary. Writable access by an unintended local
principal becomes code-controlled backup execution under `LocalSystem`.

Required correction:

- define the expected Windows ACLs in one implementation module;
- apply protected ACLs during elevated deployment without following reparse points;
- verify effective owner/DACL before starting the service;
- fail deployment closed if ACL application or read-back verification fails;
- document how ACLs are restored on a clean disaster-recovery host.

Acceptance evidence:

- unit tests validate the intended security descriptors and target allowlist;
- an elevated acceptance script verifies actual ACLs on a disposable Stable tree;
- deployment refuses an intentionally weakened ACL until it is repaired.

## CA-04 — Add production-composition coverage

**Severity:** blocking

The existing tests exercise manager classes independently. They do not start the
same composition used by `backup-manager`, which allowed placeholder callbacks to
pass all checks.

This is important because the highest-risk failures occur between components:
durable queue ownership, subprocess protocol, terminal state, cancellation, and
publication.

Required correction:

- test the public `run_service`/application boundary with injected clock and bounded
  wait controls;
- cover command, schedule, success, failure, malformed executor protocol, restart,
  cancellation, and final publication;
- retain component unit tests, but do not use them as evidence of service readiness.

## CA-05 — Correct operational claims and stage status

**Severity:** high

The operations runbook describes a fully connected cooperative lifecycle while the
current production service contains placeholders. The project status also marks
Stages 0–9 complete without production-composition evidence.

This is important because an operator may trust a documented safety behavior that
is not active in the installed service.

Required correction:

- mark the project as being in pre-Stage-10 corrective integration;
- update operational statements alongside the implementation that makes them true;
- do not restore the Stage 9 completion claim until real-manager service acceptance
  passes;
- record remaining limitations in an ADR if a normative requirement is intentionally
  deferred.

## CA-06 — Pin snapshot `latest` before queueing restore

**Severity:** blocking

**Status:** corrected; automated acceptance evidence recorded

The restore contract requires manager to resolve `latest` to one concrete snapshot
ID when it accepts the command. `CommandProcessor` exposes a resolver boundary, but
the production manager has no repository resolver. The current corrective runtime
therefore rejects `latest`; simply passing it to executor would select a potentially
newer snapshot after the command has waited in the queue.

This is important because restore must be deterministic and auditable. If a backup
ahead of the restore creates a new snapshot, resolving at execution time restores a
different version than the operator selected at command acceptance.

Required correction:

- resolve repository metadata through the privileged executor/disk lifecycle rather
  than reading an offline repository directly from manager;
- preserve FIFO ordering so resolution observes repository state at command
  acceptance and cannot race a later backup;
- validate that an explicit snapshot ID belongs to the requested job as part of the
  same resolution step;
- persist only the full resolved ID in the queued restore request;
- isolate a resolver failure so it cannot block unrelated accepted commands.

Acceptance evidence:

- a queued backup after restore acceptance cannot change the restore snapshot ID;
- a backup already ahead of restore has a deterministic, documented ordering;
- unavailable repository and invalid snapshot ID reject/fail only that request;
- restart preserves the pinned full snapshot ID.

Implemented evidence:

- an internal `resolve-restore` executor operation uses the normal privileged disk
  lifecycle and restic job filters;
- resolver completion and creation of the public restore operation commit in one
  SQLite transaction while retaining the original manual FIFO timestamp;
- integration coverage proves that a later backup cannot change the pinned ID;
- a backup already ahead retains its FIFO position and is therefore visible to the
  subsequent resolver, while work accepted later cannot overtake the logical restore;
- unit coverage proves persistence across database reopen and failure isolation.

## Recommended implementation order

1. Manager composition and command-to-executor happy path.
2. Executor failure/protocol handling and durable terminal state.
3. Scheduler and cycle completion integration.
4. Deterministic restore-version resolution.
5. Cooperative stop on the real runtime.
6. Projection, deadlines, reports, notifications, and monitoring integration.
7. Stable/data ACL enforcement and elevated acceptance.
8. Repeat Stage 9 acceptance and update status.
9. Begin Stage 10 only after all corrective evidence passes.
