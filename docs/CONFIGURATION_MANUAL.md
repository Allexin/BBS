# BBS configuration manual

This manual describes how to configure an installed BBS instance. It is an operator
guide; `BACKUP_SYSTEM_DESIGN.md` remains the normative source for behaviour and safety
rules.

Examples use `<Stable>` for the deployment root. Replace placeholders deliberately;
do not copy example identities, paths, account names, or credentials into a real setup.

## 1. Configuration files

Persistent configuration is stored below `<Stable>\data\config` and is preserved by
application deployments:

| File | Purpose |
| --- | --- |
| `manager.yaml` | scheduler, monitoring, job registry and Telegram policy |
| `smart.yaml` | configured SMART allowlist and collection timeouts |
| `jobs\<job-id>.yaml` | executor settings for one job |
| `telegram.json` | Telegram transport credentials and optional proxy |

Application updates replace `app`, `.venv`, and `web`; they do not replace `data` or
`bin`. Do not put configuration or credentials into the application tree.

Configuration files may be edited while BBS is running. Changes are applied only after
a validated service restart; hot reload is not supported.

## 2. Safe edit and apply workflow

1. Make a backup copy of the file being changed.
2. Edit YAML using spaces, not tabs. Keep `schema_version: 1`.
3. From the Stable root, validate the complete configuration without stopping BBS:

   ```bat
   backupctl.bat config validate
   ```

4. If validation succeeds, apply it with the Stable restart launcher:

   ```bat
   restart-bbs.bat
   ```

   The launcher requests UAC elevation, validates again, restarts `BBS`, checks
   `SERVICE_RUNNING`, and waits for fresh manager health/status projections.

If validation fails, the running service keeps its old in-memory configuration. Fix the
reported file and validate again. Do not use `nssm restart BBS` as the normal apply
workflow because it does not perform pre-restart validation or post-restart health checks.

## 3. `manager.yaml`

A minimal structure is:

```yaml
schema_version: 1
timezone: Europe/Samara

scheduler:
  poll_seconds: 5

monitoring:
  volumes:
    poll_seconds: 60
    items: []
  smart:
    health_policies: []

jobs: []

telegram:
  enabled: false
  credentials_file: telegram.json
  daily_report_cron: '0 9 * * *'
  daily_report_timezone: Europe/Samara
  stale_manager_minutes: 10
```

`timezone` and every schedule timezone use IANA names such as `Europe/Samara`, not a
Windows timezone display name. `scheduler.poll_seconds` controls how often manager
checks commands, schedules, deadlines, and notification work.

### 3.1 Registering a job and its schedule

Every enabled executor job must have a matching entry in `manager.yaml`:

```yaml
jobs:
  - id: disk-health
    enabled: true
    display_name: Weekly physical disk short tests
    schedule:
      cron: '0 5 * * 6'
      timezone: Europe/Samara
      cycle:
        - operation: smart-test
```

The same ID must be used as:

- `manager.yaml` job `id`;
- filename `jobs\disk-health.yaml`;
- `id` inside that job file.

Cron uses five fields: minute, hour, day of month, month, day of week. The example runs
at 05:00 every Saturday in the job timezone. Changing cron, timezone, or cycle is
reconciled after restart and recalculates the next fire time.

Supported cycle operations depend on the job kind:

| Job kind | Allowed scheduled operations |
| --- | --- |
| `snapshot` or `mirror` | `backup`, `check` |
| `maintenance` | `prune` |
| `smart-test` | `smart-test` |

For a `check` cycle item, add `mode: metadata`, `subset`, or `full` as allowed by the
validated job contract. Other operations must not contain `mode`.

An optional `deadline: '08:00'` is a wall-clock completion target in the job timezone;
it is not a forced termination time.

### 3.2 Source excludes

Snapshot and mirror job files accept source-root-relative patterns in `excludes`:

```yaml
source:
  path: 'M:\torrents\LibRusEc'
excludes:
  - 'Audiolibraries\Rutracker\audio\**\*.ogg'
```

Exact paths exclude that object and all descendants. `*` and `?` match only inside one
path component; `**` as a complete component matches zero or more directory levels.
Matching is case-insensitive. Patterns are always anchored to the configured source
root, so the example does not exclude an `audio` tree elsewhere.

Absolute paths, leading separators, `..`, negation, character classes, and `**`
embedded inside another component are invalid. Use backslashes in job YAML; BBS
translates the pattern for the selected adapter. Snapshot and mirror use the same
matching contract.

### 3.3 Repository disk lifecycle

Omit `disk` for a repository on a permanently connected local disk. BBS still verifies
the configured marker and runs SMART preflight, but never changes disk online/offline
state or mount points. Add `disk` only for a separately identified carrier that BBS must
bring online for the operation and return offline afterward. A maintenance job must use
the same repository and the same lifecycle choice as its snapshot owner.

### 3.4 Source read errors

Choose the terminal result for a snapshot that restic completed with unreadable source
files:

```yaml
backup:
  host: backup-host
  tags: ['job:archive']
  read_error_result: warning
```

`warning` keeps the incomplete snapshot, runs the configured retention, and makes the
job warning visible in Web UI and Telegram. The job card shows the total unreadable-file
count and at most 10 paths; a separate counter reports any remaining paths and a link
opens the complete plain-text report. `failed`
keeps the same diagnostic and retention behavior but marks the run failed. If restic
does not create a snapshot, the run always fails.

An older snapshot may still contain a previously readable version, but only while that
snapshot remains inside the configured retention policy. A source read warning therefore
requires operator action even when the run result is not failed.

### 3.3 Excluding an accepted-risk disk from system health

Use this only when a degraded disk has a deliberately non-critical role. Copy the
stable public ID shown on its Web UI card:

```yaml
monitoring:
  volumes:
    poll_seconds: 60
    items: []
  smart:
    health_policies:
      - disk_id: disk-0123456789ab
        affects_system_health: false
        reason: Accepted degraded disk used only for temporary media
```

The ID must match `disk-` followed by twelve lowercase hexadecimal characters. The
reason is mandatory and is published in the disk card.

This policy does **not** make the disk healthy:

- its card keeps the real `warning` or `critical` status;
- SMART observations, self-tests, history, and trend detection continue;
- new regression notifications continue;
- the disk does not create a global health issue or affect the header health;
- a SMART run failed only by excluded disks does not lower job/system health, although
  the failed run and per-disk results remain visible.

Remove the entry and restart BBS to make the disk affect system health again. Do not use
an exclusion to silence an unexplained problem on a backup/source disk.

## 4. SMART configuration

`smart.yaml` controls passive SMART reads for explicitly configured disks:

```yaml
schema_version: 1
per_disk_timeout_seconds: 30
stale_after_hours: 48
disks:
  - id: configured-disk
    display_name: Configured physical disk
    identity:
      device: /dev/pd0
      serial: '<exact physical serial>'
      expected_size_bytes: 1000000000000
```

`device`, `serial`, and capacity form an identity guard. BBS refuses to treat a different
physical device as the configured disk. Never guess these values or copy them from an
example. Serial numbers and raw selectors are private configuration and are not
published in the Web UI.

`per_disk_timeout_seconds` bounds one passive smartctl query. `stale_after_hours`
controls when an old observation is no longer considered current. Once stale, the old
metrics and timestamp remain visible, but passive health becomes `unknown` and the disk
raises a system warning until a fresh observation arrives.

### 4.1 All-system SMART self-test job

The executor file `jobs\disk-health.yaml` can discover all Windows physical disks at
the beginning of a run:

```yaml
schema_version: 1
id: disk-health
kind: smart-test
display_name: Weekly physical disk short tests
target:
  mode: all-system
test_type: short
poll_seconds: 10
timeout_seconds: 900
```

Disks are tested sequentially. A failure or timeout on one disk does not prevent attempts
on the others. `timeout_seconds` applies per disk. Heavy I/O can extend a background
self-test, so schedule it in a quiet maintenance window. A timeout is a warning about an
incomplete test, not by itself proof of physical failure.

To target one configured entry from `smart.yaml` instead:

```yaml
target:
  mode: configured-disk
  disk_id: configured-disk
```

SMART reads and self-tests do not change mount points or online/offline state. Public
cards use irreversible IDs, manufacturer/model, capacity, bus type, and current user
mount points. Serial, WWN, and raw device selector are not published.

The Web UI sorts `critical`, then `warning`, `unknown`, and `healthy` disks. A card lists
the exact health reasons above its metrics table. The first absolute critical condition
(for example, non-zero pending sectors) creates an immediate notification even when it
is the first baseline; an unchanged condition is not sent repeatedly.

## 5. Telegram

Enable the transport in `manager.yaml`:

```yaml
telegram:
  enabled: true
  credentials_file: telegram.json
  daily_report_cron: '0 9 * * *'
  daily_report_timezone: Europe/Samara
  stale_manager_minutes: 10
```

Create `<Stable>\data\config\telegram.json`:

```json
{
  "bot_token": "<bot token>",
  "chat_id": "<chat or group ID>",
  "message_thread_id": null,
  "proxy_url": null
}
```

- For a normal chat or group, keep `message_thread_id` as `null`.
- For a forum topic, set it to that topic's positive integer ID.
- For direct delivery, keep `proxy_url` as `null`.
- For a proxy, use an `http://`, `https://`, `socks5://`, or `socks5h://` URL.

The credentials file must be a regular JSON file directly inside `data\config`; paths
and reparse points are rejected. Do not commit it or include it in support bundles.

## 6. Web UI through nginx

BBS publishes sanitized static files into `<Stable>\data\public`; nginx must only read
that directory and `<Stable>\web`. Start from `docs\nginx-readonly.example.conf`, replace
the placeholder Stable root and choose the required listen address/port.

The required locations are:

- `/backup-status/` for UI assets;
- `/backup-status/status.json` and `/backup-status/health.json`;
- `/backup-status/logs/` for sanitized published logs.

Keep the locations read-only, disable autoindex, and do not expose `data\config`,
`data\state`, `data\logs`, `bin`, or the Stable root. Validate nginx configuration and
reload/restart nginx using the installation's normal service procedure.

## 7. Operator commands

Run these from the Stable root:

```bat
backupctl.bat status
backupctl.bat jobs list
backupctl.bat queue list
backupctl.bat run <job-id>
backupctl.bat check <job-id> --mode subset
backupctl.bat cancel-current
```

`run <job-id>` starts the first operation in that job's configured schedule cycle. It
does not need to know whether the job performs backup, maintenance, or SMART work.
Commands are written to the durable manager spool; receiving a `command_id` confirms
publication, not completion. Use `status`, the queue, or Web UI to inspect execution.

Do not remove queued work or cancel a running operation unless you understand its cleanup
requirements. Normal cancellation is cooperative and may wait for disk lifecycle cleanup.

## 8. Troubleshooting configuration

| Symptom | Check |
| --- | --- |
| Validation reports an unknown field | YAML key is misspelled or belongs to another file/model |
| Job is missing | IDs in manager, filename, and executor job differ |
| Schedule did not change | Configuration was edited but `restart-bbs.bat` was not run |
| Telegram transport fails | JSON fields, chat/topic ID, proxy URL, and proxy availability |
| SMART disk is unknown | selector/serial/capacity mismatch or smartctl timeout |
| SMART test times out under load | retry in an idle window; keep the timeout visible in history |
| Header stays degraded after accepted-risk policy | public disk ID mismatch, invalid config, or a separate job/disk issue |
| Web UI is stale | BBS health projection, nginx alias, permissions, and browser network response |

Important diagnostics:

- `data\logs\bootstrap.jsonl` — startup and configuration failures before the database;
- `data\logs\manager-stdout.log` and `manager-stderr.log` — service streams;
- `data\state\manager.sqlite3` — local state; do not edit it manually;
- `data\public` — sanitized files actually served to the browser.

For deployment, service recovery, and shutdown procedures, use `docs\OPERATIONS.md`.
