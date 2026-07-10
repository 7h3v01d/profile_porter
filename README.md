# Profile Porter v1.1.0

Backup and restore web-browser profiles for PC migration. Windows, PySide6, single file, stdlib + Qt only.

Copyright 2026 Leon Priest (7h3v01d) — Apache 2.0

## Supported browsers

Chrome, Edge, Brave, Vivaldi, Opera, Opera GX (Chromium family) and Firefox.

## The one thing to know before migrating

**Chromium-family saved passwords, authentication cookies and some protected payment data are encrypted with Windows-bound credentials. A copied profile should not be relied on to restore those secrets on another PC.** Use browser sync or the browser's supported password export/import workflow.

Bookmarks, history, extensions, preferences and much of the profile state are included, although individual browser features may rebuild or re-sync data on first launch (extensions typically re-download their binaries).

**Firefox profiles are substantially more portable**, including saved logins when `logins.json` and `key4.db` are present. Browser-version, policy or primary-password differences may still require user intervention.

For Chromium passwords, on the **old** PC do one of:
- Turn on browser sync (Google / Microsoft account) and let it sync down on the new PC, **or**
- Export passwords to CSV (`chrome://password-manager/settings` → Export) and import on the new PC, then delete the CSV.

## Migration workflow (old PC → new PC)

1. **Old PC:** close all browsers, run `run.bat`, tick the profiles, `CREATE BACKUP`. Copy the ZIP to a USB stick.
2. **New PC:** install the same browsers. Don't sign into anything yet. Close them.
3. **New PC:** run Profile Porter, `RESTORE` tab → `OPEN ARCHIVE…`, tick the profiles, `VERIFY + RESTORE`.
4. Launch the browser, confirm bookmarks/history/extensions.
5. Sign into sync or import the password CSV.

## Safety model (v1.1 — transactional restore)

Restore is **verify → stage → activate → rollback-on-failure**:

1. **Structural validation** — manifest schema, archive format version, member-name canonicality (no absolute, drive-qualified, `..`, backslash or symlink members), duplicate-member rejection, exact member↔manifest correspondence, unknown browser IDs rejected.
2. **Full integrity verification** — every file's SHA-256 recomputed and compared, plus a chain hash that binds each entry's *pathname + size + digest* to its predecessor. A corrupted, truncated, reordered or modified archive is refused. (Also available standalone via the **VERIFY ONLY** button.)
3. **Staging** — every restore unit (profile directory, flat Opera/GX root, core file) is fully extracted into a sibling `<name>.staging_<timestamp>`. Nothing at the final targets is touched during staging; cancelling here leaves the machine exactly as it was.
4. **Activation** — a short sequence of renames per unit: existing data → `*.pre_restore_<timestamp>` (never deleted), staging → final position.
5. **Rollback** — if any activation step fails, every completed step is reversed in order and staging is cleaned up. The targets end up exactly as they were, and the failure message says so explicitly (or flags `ROLLBACK INCOMPLETE` with logged paths if the reversal itself hit an error).

Backups stream to `<name>.zip.partial` and are promoted atomically on success — a failed or cancelled backup never destroys a previous good archive.

Other details:
- Caches excluded case-insensitively (Cache, Code Cache, GPUCache, cache2, …) — typically cuts archive size by 50–90%.
- Source file modification times are preserved through backup and restore.
- Symlinks/reparse points are never followed; they're skipped and logged.
- Refuses to touch a running browser; if the running-process check itself fails, you're warned instead of it silently passing.
- Firefox `installs.ini` is backed up but **not** restored — it binds installation paths on the old machine and Firefox regenerates it.
- `python profile_porter.py --scan` prints detection as JSON without the GUI (works without PySide6).

## Run

```
pip install PySide6
run.bat            (or: python profile_porter.py)
```

## Test

```
test.bat           (or: python -m pytest test_profile_porter.py -v)
```

25 tests, stdlib + pytest only — no Qt required. Covers the round trip (Chrome multi-profile, Opera flat, Firefox), cache exclusion incl. case variants, mtime preservation, and rejection of: tampered members, tampered digests, tampered/reordered chains, missing/undeclared/duplicate members, traversal and absolute member names, sibling-prefix containment escapes, unknown browser IDs, malformed and future-format manifests. Plus: flat-profile rename-aside, fatal preservation failure with full rollback (including an already-activated unit), staging-cancel leaving targets untouched, no staging leftovers on success, VERIFY ONLY accepting good and rejecting tampered archives, `.partial` protection of previous archives, and progress accounting for core files.

## Changelog

### 1.1.0 — transactional restore
Stage-then-activate restore with automatic rollback: profiles are extracted into `*.staging_<ts>` siblings, activated by rename, and every completed step is reversed if anything fails — a half-restored profile is no longer a reachable state. Added VERIFY ONLY (full integrity check without restoring), OPEN FOLDER buttons on backup/restore completion, and a restore summary (units / files / bytes / items set aside). 25 tests.

### 1.0.2 — scan performance
One `tasklist` snapshot shared per scan instead of one subprocess per browser (7x fewer process spawns on RESCAN and before backup/restore). Test suite stubs the snapshot entirely — runtime drops from ~2:30 to seconds on Windows. No behavioral changes.

### 1.0.1 — restore safety patch
Full verify-before-restore; hardened path containment; flat-profile (Opera/GX) safety rename; fatal preservation failures; duplicate-member and schema validation; `ARCHIVE_FORMAT_VERSION`; bound chain hash; `.partial` archives; mtime preservation; casefold exclusions; process-check failures surfaced; partial-skip confirmation; core/GUI split (engines are Qt-free); `installs.ini` restore skip; softened documentation claims.

### 1.0.0
Initial release.
