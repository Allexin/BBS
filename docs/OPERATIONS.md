# BBS operations

This runbook supplements, but does not replace, `BACKUP_SYSTEM_DESIGN.md`.

## Validate configuration

Run validation from the Stable environment before starting or restarting the service:

```powershell
C:\BackupSystem\Stable\.venv\Scripts\python.exe -m backup_system.manager --config C:\BackupSystem\Stable\data\config\manager.yaml --validate-only
```

Validation performs no disk lifecycle and queues no operation. Exit code `40` means
invalid manager or job configuration. Bootstrap failures are also appended durably to
`data\logs\bootstrap.jsonl`; exit code `41` means that bootstrap itself or its durable
diagnostic failed. NSSM is configured not to restart either exit code.

## Service lifecycle

The `BBS` service runs as `LocalSystem`. Use an elevated terminal for service control:

```powershell
nssm status BBS
nssm stop BBS
nssm start BBS
```

Stop is intentionally unbounded. Manager stops accepting commands and schedules,
discards its queued tail, sends `cancel` to the active executor, waits for executor
cleanup and confirmed disk-offline processing, publishes final status, then exits.
Do not replace this with `taskkill` during normal operation.

If manager dies unexpectedly, its Job Object terminates the executor and descendants.
On restart, startup reconciliation marks the run interrupted and may engage the safety
latch. Inspect the failed run and physical disk state before issuing the documented
`backupctl recover <job-id>` command.

## Dev-to-Stable deployment

Stable must already contain `backup-system.root`, `data\config\manager.yaml`, pinned
native tools in `bin`, and an approved NSSM executable. From Dev run:

```powershell
python -m backup_system.deployment.deploy --stable C:\BackupSystem\Stable --nginx-account <service-account>
```

The tool requests UAC elevation, waits for cooperative service stop, copies only the
release manifest, builds a new frozen non-editable `.venv`, validates Stable config,
switches application files, verifies NSSM settings, and starts the service. Stable
`data` and `bin` are never replaced. Dirty Dev files are deployed as-is and the Git
revision is reported for traceability.

`--nginx-account` is mandatory and must name the Windows account used by nginx.
Deployment protects Stable and `data`, grants that account read access only to
`data\public`, and verifies the resulting DACL before BBS is started.

There is no automatic rollback. A failure after the switch leaves the new files and
the actual stopped/failed service state visible. Correct the cause and deploy again.

## Important logs and state

- `data\logs\bootstrap.jsonl`: fatal pre-database startup diagnostics.
- `data\logs\manager-stdout.log` and `manager-stderr.log`: NSSM-captured streams.
- `data\state\manager.sqlite3`: local queue/history state, not backup truth.
- `data\public`: sanitized read-only web projection.

Never send job configs, passphrases, Telegram secrets, or private paths with a support
bundle. Preserve failed restore directories containing `.restore-incomplete` until the
failure has been investigated.
