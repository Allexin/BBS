# BBS disaster recovery

This procedure is for loss of the manager host, SQLite, or Stable application tree.
Repository contents and mirror catalogs remain the authority for protected data;
`manager.sqlite3` is not required to read an intact backup.

## Material that must exist independently

- this recovery document and a known-good BBS release;
- the approved NSSM, restic, and smartctl binaries with recorded versions and hashes;
- job configuration for every repository, including disk/volume identities;
- for `encryption.mode: password`, the passphrase stored outside that encrypted
  repository and outside the lost host;
- the physical backup media.

Do not proceed if the only passphrase copy is inside its encrypted repository.

## Rebuild on clean Windows

1. Patch Windows, install the supported Python version, and verify system time.
2. Create a new Stable root with `backup-system.root` and the documented fixed layout.
3. Restore operational configs into `data\config`; apply ACLs granting write only to
   Administrators and `LocalSystem`. Restore Telegram secrets separately.
4. Put the verified native binaries in Stable `bin`. Do not fetch tools during service
   startup or a backup operation.
5. Deploy the known-good release from a separate Dev tree. Run validation-only and
   resolve every identity/configuration error before enabling schedules.
6. Attach only the intended backup disk. Compare its physical serial, capacity,
   partition GUID, volume GUID, and repository marker with the recovered job config.
7. Inspect snapshot repositories with pinned restic or validate mirror catalogs. Do
   not initialize, repair, prune, or write merely because SQLite is absent.
8. Restore one known file into a new empty target, verify its contents, then restore
   the required subtree or source into another new target. Never restore over a live
   source.
9. Configure NSSM through the deployment workflow and verify the service runs as
   `LocalSystem`. Keep schedules disabled until restore evidence and disk identity are
   accepted by the operator.
10. Re-enable jobs deliberately. The new SQLite history starts fresh; do not copy a
    live or uncertain old database into the rebuilt service.

If repository inspection reports corruption, preserve the original medium and logs.
Work on a clone where possible and choose repair manually; BBS must not silently turn
a damaged repository into a new empty one.
