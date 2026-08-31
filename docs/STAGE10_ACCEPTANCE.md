# Stage 10 acceptance

This document tracks the reproducible end-to-end acceptance of BBS v1. It supplements
the normative criteria in `BACKUP_SYSTEM_IMPLEMENTATION_STAGES.md`; it does not add
production requirements.

## Safety boundary

- Automated acceptance runs only in Dev with generated configuration and data.
- Hardware or destructive scenarios may use only the explicitly disposable disk D.
- Automated tests do not inspect or change Stable.
- One-off Stable checks are manual acceptance steps and contain no production jobs or
  real source data.

## Evidence matrix

| Criterion | Automated evidence | Machine evidence | State |
| --- | --- | --- | --- |
| Manager, queue and executor integration | `tests/integration/test_manager_application.py` | Stage 9 corrective acceptance | Passed |
| Accelerated weekly queue and cycle | `tests/integration/test_stage10_weekly_cycle.py` | Not required | Passed |
| Snapshot backup/check/failure | Stage 7 integration tests | `poc/stage7` and Stage 0 fault probes | Passed |
| Mirror backup/check/failure | Stage 6 tests | `poc/stage6` | Passed |
| Snapshot and mirror restore | Stage 8 tests | `poc/stage8` | Passed |
| Recover and interrupted-run safety | recovery and manager integration tests | Stage 9 service acceptance | Passed |
| SMART and Web UI | SMART/projection/contract tests | `poc/stage10/run_smart_web_acceptance.ps1` | Passed during Stage 10 |
| Runtime journal and Logs UI | manager integration, journal/log projection and Web contract tests | Runtime composition correction after project review | Passed after corrective R2 |
| Clean-host recovery by runbook | Documentation review | `poc/stage10/run_disaster_recovery_acceptance.ps1` | Passed |
| Known limitations disposition | `docs/adr/0001` and `docs/adr/0002` | Not required | Passed |

The complete non-hardware suite currently passes with 405 tests and 8 explicitly
skipped hardware tests. Ruff and strict mypy also pass. Rerun all guarded machine
checks from elevated PowerShell with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\stage10\run_full_hardware_acceptance.ps1
```

The wrapper resolves D to a non-system physical disk before starting, prints progress
for every reused acceptance harness, stops on the first failure, and writes both JSON
and a transcript below `.poc-work\stage10`.

## Current machine evidence

The guarded hardware suite passed on 2026-08-31 using only disposable physical disk 2,
drive D. Stage 9 additionally observed a 12.15-second cooperative cleanup, one start of
the intentionally invalid service, and final `SERVICE_STOPPED` without a restart loop.

The disaster-recovery harness then created a password-protected repository on D,
removed its generated host/runtime tree, completed a full repository read, and restored
two files (1056 logical bytes) with matching hashes. No manager SQLite or lost-host
state was used. The expected scrub-cursor-reset warning was observed after the full
read.

## Corrective runtime-composition evidence

The post-Stage-10 project review found that `JournalWriter` and
`LogProjectionPublisher` existed but were not instantiated by the production manager.
Corrective stage R2 connected both components to the manager event flow. The integration
test now executes a manager command and verifies the complete observable path:

```text
operation/run events -> daily private JSONL -> sanitized public day -> logs/index.json
```

Progress events remain in SQLite/status projection and are intentionally excluded from
the durable JSONL journal. Public log records expose only allowlisted fields. Startup
interruptions, stage changes, classified warnings/errors and terminal results are
durable; raw executor diagnostics, credentials, repository paths and disk identities
are not copied into public projections.

## Completion rule

Stage 10 closes only when every row is passed in the current revision, machine results
contain no real paths or data in Git, and remaining limitations are fixed or recorded in
an ADR. Production jobs are still out of scope until that closure is committed.
