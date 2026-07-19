# Portable Backup and Recovery

Faerie Fire has two distinct backup systems:

- The older `backup_memory()` checkpoint keeps a rotating local copy of
  `memory.db`. It is useful for small local mistakes, but it is not a portable
  disaster-recovery backup.
- The portable instance backup creates an encrypted `.ffbackup` containing the
  complete personal profile needed to move to another Windows PC or Windows
  user.

## Set up portable backups

Faerie Fire offers backup setup on its own once the profile holds enough to
hurt if lost — about ten saved memories, fifteen chat messages, or a dozen
investigation notes, whichever comes first. The prompt appears shortly after
startup, can be snoozed for a few days, or turned off permanently with
**Don't ask again**; it never returns once backups are configured.

Open **Settings & Tools → Backup & Restore**, choose an absolute primary
destination, and create a recovery passphrase. A secondary external or
cloud-synced folder is optional. Keep the passphrase in a password manager:
there is no reset path or recovery backdoor.

The passphrase is used only to wrap a random 256-bit repository key. Windows
DPAPI caches that random key for unattended backups; neither the passphrase nor
an API credential is written to configuration, command-line arguments, logs,
or the archive payload.

Once configured, Faerie Fire registers a per-user Windows task for 20:00 local
time. A missed task starts when Windows next permits it. The desktop app also
checks on startup and retries a transient failure hourly while it stays open.
Use **Back up now** or `tools/backup_instance.py create` for an immediate copy.

Default retention is 14 daily, 4 weekly, and 12 monthly generations. The
primary destination is required. A secondary destination is a mirror, and the
app warns if both folders are on the same volume.

## Command line and launchers

The same operations are available without opening Settings:

```powershell
python tools/backup_instance.py status
python tools/backup_instance.py create
python tools/backup_instance.py scheduled
python tools/backup_instance.py restore D:\Backups\faerie-fire-....ffbackup
python tools/backup_instance.py schedule install
python tools/backup_instance.py schedule status
```

`scheduled` performs the task/startup due check and exits without creating an
extra generation when the repository is current. `restore` shows archive
metadata, securely prompts for the recovery passphrase, validates staging, and
then asks before replacing the profile; `--yes` supplies only that replacement
confirmation. A passphrase is never accepted as a command argument or
environment variable. **Portable Backup.bat** and **Restore Backup.bat** are the
double-click wrappers.

## What a portable backup contains

The encrypted payload includes:

- online SQLite snapshots of `data/memory.db` and
  `data/living_computer.db`, including open-WAL changes;
- the original automatic database secret and `secret.salt`, inside the
  authenticated encrypted payload;
- portrait files, projects and their history, journals and filed dumps,
  personas, legacy personal exports, and custom skills;
- portable behavioral settings such as language and blocklist, with machine
  paths and credentials removed; and
- a manifest with content hashes, application/build version, backup format,
  privacy epoch, and database integrity metadata.

Browser permissions and browser tasks are cleared from the staged database.
External authorization state is not carried forward. Screenshots in
`data/blobs/` are excluded by default; their activity and OCR rows remain, but
staged blob paths are cleared. Staged databases are vacuumed after scrubbing.

The following are excluded: browser profiles, diagnostics, reports, caches,
locks, `.env`, API-key files, Anthropic/Notion credentials, and other raw
machine-specific secrets. Code should continue to be backed up through Git.

## Archive safety

Archives are named `faerie-fire-<UTC>-<id>.ffbackup`. Faerie Fire snapshots
SQLite through its online backup API, checks changing project/journal inputs,
rejects links escaping configured roots, compresses the staged profile, and
streams it through AES-256-GCM. The passphrase wrapper uses Scrypt with
`N=2^17`, `r=8`, `p=1` and a fresh 16-byte salt. Each encrypted payload uses a
fresh 12-byte nonce.

The bounded public header is authenticated. Manifests, content hashes,
database key material, and the database salt remain inside the encrypted
payload. A generation is flushed, fully decrypted and validated while it is
still a `.partial`, and only then atomically renamed into place.

Portable format v1 supports Windows automatic-DPAPI database encryption.
Backup and restore stop safely when `LIVINGPC_DB_KEY`, a custom key/salt path,
disabled automatic encryption, or an unsupported platform is detected.

## Restore on a replacement PC

1. Install the same or a newer compatible Faerie Fire build.
2. Choose **Restore from backup** on the first-run screen, before entering an
   API key or creating a Soul. Restore is also available under Settings.
3. Select the `.ffbackup` and enter the recovery passphrase.
4. Review the whole-profile replacement warning and confirm.
5. Reconnect Anthropic, Notion, and browser integrations after activation.

Restore never merges databases. It decrypts into target-volume staging and
validates GCM authentication, safe paths, manifest hashes, supported versions,
SQLite integrity, and encrypted-content readability before touching the live
profile. On the new PC it DPAPI-protects the original automatic database secret
for the new Windows user and preserves the original database salt. Portrait
and optional screenshot paths are rebased to the new instance.

For a non-empty target, Faerie Fire first creates a verified encrypted rollback
snapshot. Activation happens after GUI/database connections close. A failed
activation is reversed automatically, and a successful rollback generation is
kept for seven days. External integrations remain paused and overdue reminders
stay suppressed until reviewed.

A newer unsupported archive is blocked with an “update Faerie Fire first”
message. Same-version and supported older archives are migrated in staging.

## Forget and backup privacy

Backup, restore, and explicit Forget share one cross-process maintenance lock.
Each managed repository records a privacy epoch. Forget advances that epoch,
removes every managed generation from both destinations, and creates a new
post-Forget baseline. If a destination is offline, Faerie Fire records a
durable purge-pending state, displays a warning, and blocks new uploads or
restore from that repository until stale generations are removed.

Faerie Fire can revoke only archives it manages in configured destinations.
Manually copied `.ffbackup` files and a cloud provider’s deleted-file or
version history are outside its control. Remove those separately when the
right-to-forget boundary matters.

