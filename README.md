# Profile Porter v1.0.0

Backup and restore web-browser profiles for PC migration. Windows, PySide6, single file, stdlib + Qt only.

Copyright 2026 Leon Priest (7h3v01d) — Apache 2.0

## Supported browsers

Chrome, Edge, Brave, Vivaldi, Opera, Opera GX (Chromium family) and Firefox.

## The one thing to know before migrating

**Chromium browsers encrypt saved passwords and cookies with Windows DPAPI, keyed to the old machine + user account. They cannot be decrypted on the new PC — by design, no tool can move them.** Everything else transfers: bookmarks, history, extensions, settings, autofill, open tabs, pinned sites.

For passwords, on the **old** PC do one of:
- Turn on browser sync (Google / Microsoft account) and let it sync down on the new PC, **or**
- Export passwords to CSV (`chrome://password-manager/settings` → Export) and import on the new PC, then delete the CSV.

**Firefox transfers completely** — passwords included (`logins.json` + `key4.db` travel with the profile).

## Migration workflow (old PC → new PC)

1. **Old PC:** close all browsers, run `run.bat`, tick the profiles, `CREATE BACKUP`. Copy the ZIP to a USB stick.
2. **New PC:** install the same browsers. Don't sign into anything yet. Close them.
3. **New PC:** run Profile Porter, `RESTORE` tab → `OPEN ARCHIVE…`, tick the profiles, `RESTORE SELECTED`.
4. Launch the browser. Bookmarks/history/extensions should be there (extensions may re-download their binaries on first launch).
5. Sign into sync or import the password CSV.

Restore never deletes anything — existing data at the target is renamed to `*.pre_restore_<timestamp>` first. Once the new PC is confirmed good, those folders can be removed manually.

## Details

- Caches are excluded from backup (Cache, Code Cache, GPUCache, cache2, etc.) — typically cuts archive size by 50–90%.
- Archive is a plain ZIP with a `manifest.json` containing per-file SHA-256 and a chain hash over the whole file set.
- Refuses to back up or restore a browser that is currently running.
- Target root per browser is auto-detected on the new machine and editable in the Restore tab (select the browser row).
- `python profile_porter.py --scan` prints detection as JSON without launching the GUI.

## Run

```
pip install PySide6
python profile_porter.py
```

## Test

```
python test_roundtrip.py
```

Headless round trip: fakes Chrome + Firefox layouts, backs up, restores to a fresh root, verifies contents, cache exclusion, manifest chain, and the safety rename.
