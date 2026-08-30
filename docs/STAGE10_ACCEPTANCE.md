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
| Snapshot backup/check/failure | Stage 7 integration tests | `poc/stage7` and Stage 0 fault probes | Passed previously; rerun pending |
| Mirror backup/check/failure | Stage 6 tests | `poc/stage6` | Passed previously; rerun pending |
| Snapshot and mirror restore | Stage 8 tests | `poc/stage8` | Passed previously; rerun pending |
| Recover and interrupted-run safety | recovery and manager integration tests | Stage 9 service acceptance | Passed previously; rerun pending |
| SMART and Web UI | SMART/projection/contract tests | `poc/stage10/run_smart_web_acceptance.ps1` | Passed during Stage 10 |
| Clean-host recovery by runbook | Documentation review | Disposable clean environment | Pending |
| Known limitations disposition | ADR and documentation review | Not required | Pending |

The complete non-hardware suite currently passes with 369 tests and 8 explicitly
skipped hardware tests. Ruff and strict mypy also pass. Rerun all guarded machine
checks from elevated PowerShell with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\stage10\run_full_hardware_acceptance.ps1
```

The wrapper resolves D to a non-system physical disk before starting, prints progress
for every reused acceptance harness, stops on the first failure, and writes both JSON
and a transcript below `.poc-work\stage10`.

## Completion rule

Stage 10 closes only when every row is passed in the current revision, machine results
contain no real paths or data in Git, and remaining limitations are fixed or recorded in
an ADR. Production jobs are still out of scope until that closure is committed.
