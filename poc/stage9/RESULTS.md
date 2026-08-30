# Stage 9 acceptance

Automated unit and integration coverage verifies bootstrap diagnostics, cooperative
shutdown ordering, Job Object kill-on-close, release staging, and NSSM configuration
read-back.

Machine-level acceptance passed on NSSM 2.24 (64-bit):

- cooperative stop waited 12.05 seconds for the fixture cleanup and did not hard-kill it;
- manager config exit code `40` started exactly once and remained `SERVICE_STOPPED`;
- temporary acceptance services were removed;
- no backup disk was accessed.

The reproducible command is:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\poc\stage9\run_service_acceptance.ps1
```

The harness creates uniquely named disposable Windows services, does not access any
backup disk, removes the services in `finally`, and saves its result under
`.poc-work/stage9/service-result.json`.

NSSM 2.24 does not expose `AppKillProcessTree` through its CLI. The harness follows
the NSSM documentation by writing that one `REG_DWORD` directly below the temporary
service's `Parameters` key, flushing it, and checking the value before service start.
