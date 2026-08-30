# Corrective acceptance results

## Stable ACL policy

Status: pending elevated execution.

Run from an elevated PowerShell in the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\corrective\run_acl_acceptance.ps1
```

The test creates and removes only a uniquely named tree below
`.poc-work\corrective-acl`. It does not access backup disks or Windows services.
The machine-readable result is saved to
`.poc-work\corrective-acl\acl-result.json`.

## Telegram transport

Dev acceptance reads only `secrets\telegram.json`, which is ignored by Git. It does
not read or modify Stable configuration. Run without elevation:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\corrective\run_telegram_acceptance.ps1
```

The result contains no credentials and is saved to
`.poc-work\corrective-telegram\telegram-result.json`.
