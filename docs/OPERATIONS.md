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

For a normal configuration change, run the validated launcher from the Stable root in
an elevated terminal. It validates the complete Stable config before touching the service,
then requires both `SERVICE_RUNNING` and a fresh matching health/status projection from
the new manager process:

```bat
restart-bbs.bat
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

For ordinary subsequent updates, stop BBS from an elevated terminal and then run
the guarded wrapper from a normal Dev PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\corrective\run_stable_update.ps1 -Stable S:\BasovBackupSystem\BBS_Stable
```

The wrapper requires an approved `nssm.exe` in PATH. It looks for uv in
Dev `.venv\Scripts\uv.exe`, `.poc-work\tools\uv\uv.exe`, and then PATH.
It refuses to proceed unless the service is already stopped, copies only the release
manifest, and builds a new frozen non-editable `.venv`. It replaces only the contents
of `app`, `.venv`, and `web`, then asks the operator to start the service manually and
waits for `SERVICE_RUNNING`. Stable
`data` and `bin` are never replaced. Dirty Dev files are deployed as-is and the Git
revision is reported for traceability.

`--nginx-account` is mandatory and must name the Windows account used by nginx.
One-time administrative bootstrap protects Stable and `data`, grants the nginx
account read access only to `data\public`, and grants the explicitly trusted local
developer account write access only to `app`, `.venv`, and `web`. This trust boundary
allows that account to replace code later executed as LocalSystem and is specific to
this local Dev workflow.

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

Telegram credentials are stored in the configured JSON filename below
`data\config` (normally `telegram.json`). On the explicitly approved local deployment,
the trusted configuration/deployment account may read and edit this file together with
the remaining backup configuration. Treat that account as a backup-system operator.
Use `message_thread_id: null` for a normal chat or a positive topic ID for a forum
topic. Use `proxy_url: null` for a direct connection or an HTTP(S)/SOCKS5 URL when a
proxy is required. Never commit this file or include it in a support bundle.
