# Stage 9 acceptance

Automated unit and integration coverage verifies bootstrap diagnostics, cooperative
shutdown ordering, Job Object kill-on-close, release staging, and NSSM configuration
read-back.

The machine-level NSSM acceptance is pending. Place the approved `nssm.exe` at
`.tools/nssm/nssm.exe`, then run elevated:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\stage9\run_service_acceptance.ps1
```

The harness creates uniquely named disposable Windows services, does not access any
backup disk, removes the services in `finally`, and saves its result under
`.poc-work/stage9/service-result.json`.
