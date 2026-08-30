# Corrective acceptance results

## Real manager service

Status: passed on the Stable composition.

- NSSM reached `SERVICE_RUNNING` under LocalSystem;
- manager published `idle` health/status projections with no bootstrap diagnostics;
- manual NSSM stop reached `SERVICE_STOPPED` and manager published the final
  `stopping` projection;
- the acceptance configuration contained no jobs and accessed no backup disk.

## Stable ACL policy

Status: passed against the actual test Stable tree.

- protected root, `data`, and `data/public` descriptors passed semantic read-back;
- the explicitly trusted local deployment account may manage `data\config`, including
  `telegram.json`, as an accepted local security boundary;
- its application write access remains limited to `app`, `.venv`, and `web`; `bin`,
  state, and logs remain protected;
- non-admin application update completed successfully without changing `data` or
  `bin`.

Run from an elevated PowerShell in the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\corrective\run_acl_acceptance.ps1
```

The test creates and removes only a uniquely named tree below
`.poc-work\corrective-acl`. It does not access backup disks or Windows services.
The machine-readable result is saved to
`.poc-work\corrective-acl\acl-result.json`.

## Telegram transport

Status: passed; the test notification was delivered through the configured proxy.

Dev acceptance reads only `secrets\telegram.json`, which is ignored by Git. It does
not read or modify Stable configuration. Run without elevation:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\corrective\run_telegram_acceptance.ps1
```

The result contains no credentials and is saved to
`.poc-work\corrective-telegram\telegram-result.json`.
