#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Profile Porter v1.0.2
# Backup / restore web-browser profiles for PC migration.
#
# Copyright 2026 Leon Priest (7h3v01d)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# ---------------------------------------------------------------------------
"""
Profile Porter — governed backup & restore of browser profiles (Windows).

Supported: Chrome, Edge, Brave, Vivaldi, Opera, Opera GX, Firefox.

v1.0.1 restore-safety patch:
  * Full archive verification (schema, member names, per-file SHA-256,
    bound chain hash) BEFORE any change is made on disk.
  * Path containment via Path.is_relative_to(); absolute / drive-qualified /
    dot-dot / backslash / symlink ZIP members rejected outright.
  * Flat profiles (Opera / Opera GX) now get the same rename-aside safety
    treatment as named Chromium profiles.
  * Any failed safety rename ABORTS the restore before a byte is written.
  * Duplicate ZIP members rejected; unknown browser IDs rejected;
    ARCHIVE_FORMAT_VERSION separated from the application version.
  * Backups stream to <name>.zip.partial and are promoted on success, so a
    failed run never destroys a previous good archive.
  * Chain hash binds arc-name + size + digest, not the digest alone.
  * Core engine is Qt-free: tests run with the standard library only.

Layers in this file (single-file distribution, layered design):
  [1] catalog + discovery          (stdlib)
  [2] BackupEngine / RestoreEngine + archive validator   (stdlib)
  [3] CLI scan mode                (stdlib)
  [4] Qt worker adapters + GUI     (PySide6, optional at import time)

CLI: `python profile_porter.py --scan` prints detection JSON, no GUI.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

APP_NAME = "Profile Porter"
APP_VERSION = "1.0.2"
ARCHIVE_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
CHUNK = 1024 * 1024  # 1 MiB streaming chunks
GENESIS = "0" * 64

# =====================================================================
# [1] catalog + discovery
# =====================================================================

_CHROMIUM_SKIP_DIRS = {
    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
    "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "CacheStorage",
    "ScriptCache", "Media Cache", "Crashpad", "Crash Reports",
    "component_crx_cache", "GraphiteDawnCache", "optimization_guide_model_store",
    "BrowserMetrics", "Safe Browsing",
}
_FIREFOX_SKIP_DIRS = {
    "cache2", "startupCache", "crashes", "minidumps", "thumbnails",
    "shader-cache", "saved-telemetry-pings",
}
_SKIP_FILES = {
    "lockfile", "parent.lock", ".parentlock",
    "SingletonCookie", "SingletonLock", "SingletonSocket",
    "LOCK",  # leveldb lock stubs — regenerated
}

# Windows filesystems are case-insensitive; match exclusions the same way.
CHROMIUM_SKIP_DIRS = {d.casefold() for d in _CHROMIUM_SKIP_DIRS}
FIREFOX_SKIP_DIRS = {d.casefold() for d in _FIREFOX_SKIP_DIRS}
SKIP_FILES = {f.casefold() for f in _SKIP_FILES}

# Core files that are backed up but deliberately NOT restored:
# installs.ini binds Firefox *installation paths* to profiles and is
# machine-specific; Firefox regenerates it cleanly on first launch.
SKIP_RESTORE_CORE_BASENAMES = {"installs.ini"}


@dataclass
class BrowserSpec:
    bid: str
    name: str
    family: str            # "chromium" | "chromium_flat" | "firefox"
    root: Path
    processes: list[str]

    @property
    def skip_dirs(self) -> set[str]:
        return FIREFOX_SKIP_DIRS if self.family == "firefox" else CHROMIUM_SKIP_DIRS


def build_specs() -> list[BrowserSpec]:
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    roam = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
    return [
        BrowserSpec("chrome", "Google Chrome", "chromium",
                    local / "Google/Chrome/User Data", ["chrome.exe"]),
        BrowserSpec("edge", "Microsoft Edge", "chromium",
                    local / "Microsoft/Edge/User Data", ["msedge.exe"]),
        BrowserSpec("brave", "Brave", "chromium",
                    local / "BraveSoftware/Brave-Browser/User Data", ["brave.exe"]),
        BrowserSpec("vivaldi", "Vivaldi", "chromium",
                    local / "Vivaldi/User Data", ["vivaldi.exe"]),
        BrowserSpec("opera", "Opera", "chromium_flat",
                    roam / "Opera Software/Opera Stable", ["opera.exe"]),
        BrowserSpec("operagx", "Opera GX", "chromium_flat",
                    roam / "Opera Software/Opera GX Stable", ["opera.exe"]),
        BrowserSpec("firefox", "Mozilla Firefox", "firefox",
                    roam / "Mozilla/Firefox", ["firefox.exe"]),
    ]


def spec_by_id() -> dict[str, BrowserSpec]:
    return {s.bid: s for s in build_specs()}


def iter_files(root: Path, skip_dirs: set[str],
               on_skip: Callable[[str], None] | None = None):
    """Yield regular files under root, pruning cache/lock noise.
    Symlinks / reparse points are refused and reported, never followed."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for d in dirnames:
            if d.casefold() in skip_dirs:
                continue
            if (Path(dirpath) / d).is_symlink():
                if on_skip:
                    on_skip(f"symlinked dir skipped: {Path(dirpath) / d}")
                continue
            kept.append(d)
        dirnames[:] = kept
        for fn in filenames:
            if fn.casefold() in SKIP_FILES:
                continue
            p = Path(dirpath) / fn
            if p.is_symlink():
                if on_skip:
                    on_skip(f"symlink skipped: {p}")
                continue
            yield p


def dir_stats(root: Path, skip_dirs: set[str]) -> tuple[int, int]:
    files = 0
    total = 0
    for p in iter_files(root, skip_dirs):
        try:
            total += p.stat().st_size
            files += 1
        except OSError:
            pass
    return files, total


def human_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:,.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{n} B"


@dataclass
class ProcCheck:
    running: list[str]
    ok: bool                 # False => the check itself could not be completed


def tasklist_snapshot() -> tuple[str, bool]:
    """One `tasklist` invocation, shared across a whole scan — spawning a
    subprocess per browser makes rescans (and the test suite) crawl."""
    if platform.system() != "Windows":
        return "", True      # detection paths are Windows-only anyway
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.lower()
        return out, True
    except Exception:
        return "", False


def check_processes(names: list[str],
                    snapshot: tuple[str, bool] | None = None) -> ProcCheck:
    """Which of the given executables are running (Windows). If the check
    cannot be performed, that is reported — never silently treated as
    'nothing running'."""
    out, ok = snapshot if snapshot is not None else tasklist_snapshot()
    if not ok:
        return ProcCheck([], False)
    return ProcCheck([n for n in names if f'"{n.lower()}"' in out], True)


@dataclass
class ProfileInfo:
    folder: str           # rel path from browser root ("" = flat/whole-root)
    display: str
    path: Path
    files: int = 0
    bytes: int = 0


@dataclass
class BrowserState:
    spec: BrowserSpec
    profiles: list[ProfileInfo] = field(default_factory=list)
    core_files: list[str] = field(default_factory=list)   # rel paths from root
    proc: ProcCheck = field(default_factory=lambda: ProcCheck([], True))

    @property
    def total_bytes(self) -> int:
        return sum(p.bytes for p in self.profiles)


def _chromium_display_names(root: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        data = json.loads((root / "Local State").read_text(encoding="utf-8"))
        for folder, meta in data.get("profile", {}).get("info_cache", {}).items():
            nm = meta.get("name") or meta.get("gaia_name")
            if nm:
                names[folder] = nm
    except Exception:
        pass
    return names


def discover_browser(spec: BrowserSpec, with_sizes: bool = True,
                     on_log: Callable[[str], None] | None = None,
                     snapshot: tuple[str, bool] | None = None
                     ) -> BrowserState | None:
    root = spec.root
    if not root.is_dir():
        return None
    state = BrowserState(spec=spec,
                         proc=check_processes(spec.processes, snapshot))

    if spec.family == "chromium":
        names = _chromium_display_names(root)
        for sub in sorted(root.iterdir()):
            if not sub.is_dir() or sub.name in ("System Profile", "Guest Profile"):
                continue
            if not (sub / "Preferences").is_file():
                continue
            disp = names.get(sub.name, "")
            disp = f"{sub.name}  ({disp})" if disp else sub.name
            state.profiles.append(ProfileInfo(sub.name, disp, sub))
        for core in ("Local State", "First Run"):
            if (root / core).is_file():
                state.core_files.append(core)

    elif spec.family == "chromium_flat":
        if (root / "Preferences").is_file():
            state.profiles.append(ProfileInfo("", "(main profile)", root))

    elif spec.family == "firefox":
        ini = root / "profiles.ini"
        if not ini.is_file():
            return None
        cp = configparser.ConfigParser()
        try:
            cp.read(ini, encoding="utf-8")
        except Exception:
            return None
        for section in cp.sections():
            if not section.lower().startswith("profile"):
                continue
            rel = cp.get(section, "Path", fallback=None)
            if not rel:
                continue
            if cp.get(section, "IsRelative", fallback="1") != "1":
                if on_log:
                    on_log(f"[warn] Firefox profile with absolute path is out of "
                           f"scope and was NOT included: {rel}")
                continue
            ppath = root / rel
            if not ppath.is_dir():
                continue
            disp = cp.get(section, "Name", fallback=rel)
            state.profiles.append(
                ProfileInfo(rel.replace("\\", "/"), f"{disp}  ({rel})", ppath))
        for core in ("profiles.ini", "installs.ini"):
            if (root / core).is_file():
                state.core_files.append(core)

    if not state.profiles:
        return None
    if with_sizes:
        for p in state.profiles:
            p.files, p.bytes = dir_stats(p.path, spec.skip_dirs)
    return state


def scan_all(with_sizes: bool = True,
             on_log: Callable[[str], None] | None = None) -> list[BrowserState]:
    snap = tasklist_snapshot()
    found = []
    for spec in build_specs():
        st = discover_browser(spec, with_sizes=with_sizes, on_log=on_log,
                              snapshot=snap)
        if st:
            found.append(st)
    return found

# =====================================================================
# [2] engines + archive validation  (stdlib only — no Qt)
# =====================================================================


class ArchiveError(ValueError):
    """The archive or its manifest failed validation/verification."""


class RestoreAborted(RuntimeError):
    """A safety precondition failed; nothing (further) was written."""


class Cancelled(Exception):
    pass


def _noop(*_a, **_k):
    pass


def chain_next(prev: str, arc: str, size: int, digest: str) -> str:
    """Chain record binds pathname + size + digest, so a manifest entry cannot
    be reassigned to another path while keeping the same file digest."""
    record = prev + "\0" + arc + "\0" + str(size) + "\0" + digest
    return hashlib.sha256(record.encode("utf-8")).hexdigest()


def validate_member_name(name: str) -> None:
    """Reject anything that is not a canonical, contained, forward-slash
    relative path of the form <bid>/root/<...>."""
    if "\\" in name:
        raise ArchiveError(f"backslash in member name: {name!r}")
    if ":" in name:
        raise ArchiveError(f"drive-qualified member name: {name!r}")
    if name.startswith("/"):
        raise ArchiveError(f"absolute member name: {name!r}")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ArchiveError(f"illegal path component in member name: {name!r}")
    if len(parts) < 3 or parts[1] != "root":
        raise ArchiveError(f"member outside <browser>/root/: {name!r}")


def _is_symlink_member(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def load_and_validate_manifest(zf: zipfile.ZipFile) -> dict:
    """Structural validation. Raises ArchiveError with a precise reason.
    Does NOT hash file contents — see verify_archive_hashes()."""
    names = zf.namelist()

    # duplicates make zf.open()/getinfo() ambiguous — refuse outright
    seen: set[str] = set()
    for n in names:
        if n in seen:
            raise ArchiveError(f"archive contains duplicate member: {n!r}")
        seen.add(n)

    if MANIFEST_NAME not in seen:
        raise ArchiveError(f"{MANIFEST_NAME} missing — not a {APP_NAME} archive")
    try:
        manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
    except Exception as e:
        raise ArchiveError(f"{MANIFEST_NAME} is not valid JSON: {e}") from e

    if not isinstance(manifest, dict) or manifest.get("tool") != APP_NAME:
        raise ArchiveError(f"manifest 'tool' is not {APP_NAME!r}")
    fmt = manifest.get("format")
    if fmt != ARCHIVE_FORMAT_VERSION:
        raise ArchiveError(
            f"unsupported archive format {fmt!r} (this build supports "
            f"{ARCHIVE_FORMAT_VERSION}) — archive from a newer Profile Porter?")
    if not isinstance(manifest.get("browsers"), list) \
            or not isinstance(manifest.get("files"), list) \
            or not isinstance(manifest.get("chain_head"), str):
        raise ArchiveError("manifest missing browsers/files/chain_head")

    known = spec_by_id()
    seen_bids: set[str] = set()
    for b in manifest["browsers"]:
        if not isinstance(b, dict) or not isinstance(b.get("id"), str):
            raise ArchiveError("malformed browser entry in manifest")
        bid = b["id"]
        if bid in seen_bids:
            raise ArchiveError(f"duplicate browser entry: {bid!r}")
        seen_bids.add(bid)
        if bid not in known:
            raise ArchiveError(
                f"unknown browser id {bid!r} — archive from a newer "
                f"Profile Porter?")
        for arc in b.get("core", []):
            if not (isinstance(arc, str) and arc.startswith(f"{bid}/root/")):
                raise ArchiveError(f"core entry outside {bid}/root/: {arc!r}")
        for p in b.get("profiles", []):
            if not isinstance(p, dict):
                raise ArchiveError("malformed profile entry")
            folder = p.get("folder")
            prefix = p.get("arc_prefix")
            expect = f"{bid}/root" if folder == "" else f"{bid}/root/{folder}"
            if prefix != expect:
                raise ArchiveError(
                    f"profile arc_prefix {prefix!r} does not match folder "
                    f"{folder!r} for browser {bid!r}")
            if not isinstance(p.get("bytes"), int) or p["bytes"] < 0 \
                    or not isinstance(p.get("files"), int) or p["files"] < 0:
                raise ArchiveError(f"invalid size metadata for {prefix!r}")

    # every member <-> exactly one manifest entry, and every name canonical
    arcs: set[str] = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise ArchiveError("malformed file entry in manifest")
        arc, digest, size = entry.get("arc"), entry.get("sha256"), entry.get("bytes")
        if not isinstance(arc, str) or not isinstance(digest, str) \
                or not isinstance(size, int) or size < 0:
            raise ArchiveError(f"malformed file entry: {entry!r}")
        validate_member_name(arc)
        if arc.split("/", 1)[0] not in seen_bids:
            raise ArchiveError(f"file entry for undeclared browser: {arc!r}")
        if arc in arcs:
            raise ArchiveError(f"duplicate file entry in manifest: {arc!r}")
        arcs.add(arc)

    members = seen - {MANIFEST_NAME}
    if members != arcs:
        extra = sorted(members - arcs)[:5]
        missing = sorted(arcs - members)[:5]
        raise ArchiveError(
            f"manifest/members mismatch — undeclared members: {extra}, "
            f"declared but absent: {missing}")
    for n in members:
        validate_member_name(n)
        if _is_symlink_member(zf.getinfo(n)):
            raise ArchiveError(f"symlink member refused: {n!r}")

    return manifest


def verify_archive_hashes(zf: zipfile.ZipFile, manifest: dict,
                          on_progress: Callable[[str, int, int], None] = _noop,
                          cancel_check: Callable[[], bool] = lambda: False
                          ) -> None:
    """Recompute every file's SHA-256 + the bound chain. Raises ArchiveError
    on the first mismatch. Nothing on disk is touched."""
    total = sum(e["bytes"] for e in manifest["files"])
    done = 0
    chain = GENESIS
    for entry in manifest["files"]:
        if cancel_check():
            raise Cancelled
        arc = entry["arc"]
        h = hashlib.sha256()
        size = 0
        with zf.open(arc) as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
                done += len(chunk)
                on_progress("verify", done, total)
        if size != entry["bytes"]:
            raise ArchiveError(
                f"size mismatch for {arc!r}: archive {size}, manifest "
                f"{entry['bytes']} — archive corrupted or modified")
        digest = h.hexdigest()
        if digest != entry["sha256"]:
            raise ArchiveError(
                f"SHA-256 mismatch for {arc!r} — archive corrupted or modified")
        chain = chain_next(chain, arc, size, digest)
        if chain != entry.get("chain"):
            raise ArchiveError(
                f"chain mismatch at {arc!r} — manifest reordered or modified")
    if chain != manifest["chain_head"]:
        raise ArchiveError("chain head mismatch — manifest modified")


class BackupEngine:
    """Streams selected profiles into <dest>.partial, promotes to <dest> on
    success. Qt-free; progress/log/cancel via callbacks."""

    def __init__(self, jobs: list[tuple[BrowserState, list[ProfileInfo]]],
                 dest: Path,
                 on_log: Callable[[str], None] = _noop,
                 on_progress: Callable[[str, int, int], None] = _noop,
                 cancel_check: Callable[[], bool] = lambda: False):
        self.jobs = jobs
        self.dest = Path(dest)
        self.on_log = on_log
        self.on_progress = on_progress
        self.cancel_check = cancel_check

    def _add_file(self, zf: zipfile.ZipFile, src: Path, arc: str) -> tuple[str, int]:
        try:
            mtime = time.localtime(src.stat().st_mtime)[:6]
            if mtime[0] < 1980:          # zip epoch floor
                mtime = (1980, 1, 1, 0, 0, 0)
        except OSError:
            mtime = time.localtime(time.time())[:6]
        zi = zipfile.ZipInfo(arc, date_time=mtime)
        zi.compress_type = zipfile.ZIP_DEFLATED
        h = hashlib.sha256()
        size = 0
        with src.open("rb") as f, zf.open(zi, "w") as dst:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                dst.write(chunk)
                size += len(chunk)
        return h.hexdigest(), size

    def run(self) -> tuple[bool, str]:
        partial = self.dest.with_name(self.dest.name + ".partial")
        # total includes core files, so the progress bar is honest
        total = sum(p.bytes for _, plist in self.jobs for p in plist)
        for state, _ in self.jobs:
            for rel in state.core_files:
                try:
                    total += (state.spec.root / rel).stat().st_size
                except OSError:
                    pass

        done = 0
        chain = GENESIS
        files_manifest: list[dict] = []
        browsers_manifest: list[dict] = []
        skipped: list[str] = []
        self.on_log(f"[backup] writing {partial.name}")

        def emit(size: int):
            nonlocal done
            done += size
            self.on_progress("backup", done, total)

        try:
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED,
                                 allowZip64=True) as zf:
                for state, plist in self.jobs:
                    spec = state.spec
                    b_entry = {"id": spec.bid, "name": spec.name,
                               "family": spec.family,
                               "source_root": str(spec.root),
                               "core": [], "profiles": []}
                    for rel in state.core_files:
                        if self.cancel_check():
                            raise Cancelled
                        src = spec.root / rel
                        arc = f"{spec.bid}/root/{PurePosixPath(rel)}"
                        try:
                            digest, size = self._add_file(zf, src, arc)
                        except OSError as e:
                            skipped.append(f"{src} ({e})")
                            continue
                        chain = chain_next(chain, arc, size, digest)
                        files_manifest.append({"arc": arc, "sha256": digest,
                                               "bytes": size, "chain": chain})
                        b_entry["core"].append(arc)
                        emit(size)

                    for prof in plist:
                        self.on_log(f"[backup] {spec.name} :: {prof.display}")
                        pf = pb = 0
                        for src in iter_files(prof.path, spec.skip_dirs,
                                              on_skip=lambda m: self.on_log(f"[skip] {m}")):
                            if self.cancel_check():
                                raise Cancelled
                            rel2 = src.relative_to(spec.root).as_posix()
                            arc = f"{spec.bid}/root/{rel2}"
                            try:
                                digest, size = self._add_file(zf, src, arc)
                            except OSError as e:
                                skipped.append(f"{src} ({e})")
                                continue
                            chain = chain_next(chain, arc, size, digest)
                            files_manifest.append({"arc": arc, "sha256": digest,
                                                   "bytes": size, "chain": chain})
                            pf += 1
                            pb += size
                            emit(size)
                        b_entry["profiles"].append({
                            "folder": prof.folder,
                            "display": prof.display,
                            "arc_prefix": (f"{spec.bid}/root/{prof.folder}"
                                           if prof.folder else f"{spec.bid}/root"),
                            "files": pf, "bytes": pb})
                    browsers_manifest.append(b_entry)

                manifest = {
                    "tool": APP_NAME,
                    "app_version": APP_VERSION,
                    "format": ARCHIVE_FORMAT_VERSION,
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                    "host": platform.node(),
                    "user": os.environ.get("USERNAME", ""),
                    "chain_head": chain,
                    "skipped": skipped,
                    "browsers": browsers_manifest,
                    "files": files_manifest,
                }
                zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=1))
        except Cancelled:
            partial.unlink(missing_ok=True)
            return False, ("Backup cancelled — partial archive removed; any "
                           "previous archive at the destination is untouched.")
        except Exception as e:
            partial.unlink(missing_ok=True)
            return False, f"Backup failed: {e}"

        os.replace(partial, self.dest)   # atomic promote
        msg = (f"Backup complete: {len(files_manifest)} files, "
               f"{human_bytes(done)} -> {self.dest}")
        if skipped:
            msg += f"  ({len(skipped)} unreadable files skipped — see log)"
            for s in skipped[:50]:
                self.on_log(f"[skipped] {s}")
        return True, msg


class RestoreEngine:
    """Verify EVERYTHING first, then rename existing data aside (fatal on
    failure), then extract. Qt-free."""

    def __init__(self, archive: Path,
                 selections: list[tuple[str, str, Path]],       # (bid, arc_prefix, root)
                 core_map: dict[str, tuple[list[str], Path]],   # bid -> (core arcs, root)
                 on_log: Callable[[str], None] = _noop,
                 on_progress: Callable[[str, int, int], None] = _noop,
                 cancel_check: Callable[[], bool] = lambda: False):
        self.archive = Path(archive)
        self.selections = selections
        self.core_map = core_map
        self.on_log = on_log
        self.on_progress = on_progress
        self.cancel_check = cancel_check

    @staticmethod
    def _aside_path(target: Path, ts: str) -> Path:
        aside = target.with_name(f"{target.name}.pre_restore_{ts}")
        i = 1
        while aside.exists():
            aside = target.with_name(f"{target.name}.pre_restore_{ts}_{i}")
            i += 1
        return aside

    @staticmethod
    def _contained(root: Path, rel: str) -> Path:
        root_resolved = root.resolve()
        dest = (root_resolved / rel).resolve()
        if not dest.is_relative_to(root_resolved):
            raise RestoreAborted(f"path containment violated: {rel!r}")
        return dest

    def run(self) -> tuple[bool, str]:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            with zipfile.ZipFile(self.archive, "r") as zf:
                manifest = load_and_validate_manifest(zf)

                # -- phase 1: verify every byte before touching the disk
                self.on_log("[verify] checking archive integrity…")
                verify_archive_hashes(zf, manifest,
                                      on_progress=self.on_progress,
                                      cancel_check=self.cancel_check)
                self.on_log(f"[verify] integrity verified: "
                            f"{len(manifest['files']):,} files")

                # -- phase 2: build the plan against the VALIDATED manifest
                declared_prefixes = {
                    p["arc_prefix"]
                    for b in manifest["browsers"] for p in b["profiles"]}
                names = set(zf.namelist()) - {MANIFEST_NAME}

                plan: list[tuple[str, str, Path]] = []   # (member, rel, root)
                flat_roots: list[Path] = []
                named_targets: list[Path] = []
                for bid, prefix, root in self.selections:
                    if prefix not in declared_prefixes:
                        raise ArchiveError(
                            f"selected prefix {prefix!r} is not declared in "
                            f"the manifest")
                    base = f"{bid}/root/"
                    if prefix == f"{bid}/root":            # flat profile
                        hit = [n for n in names if n.startswith(base)]
                        flat_roots.append(root)
                    else:
                        hit = [n for n in names if n.startswith(prefix + "/")]
                        named_targets.append(root / prefix[len(base):])
                    for n in hit:
                        plan.append((n, n[len(base):], root))

                core_plan: list[tuple[str, str, Path]] = []
                for bid, (arcs, root) in self.core_map.items():
                    base = f"{bid}/root/"
                    for arc in arcs:
                        if arc not in names:
                            continue
                        if PurePosixPath(arc).name in SKIP_RESTORE_CORE_BASENAMES:
                            self.on_log(f"[restore] {arc} is machine-specific — "
                                        f"backed up but not restored (the browser "
                                        f"regenerates it)")
                            continue
                        core_plan.append((arc, arc[len(base):], root))

                if not plan and not core_plan:
                    return False, "Nothing matched in archive."

                total = sum(zf.getinfo(m).file_size for m, _, _ in plan + core_plan)

                # -- phase 3: safety renames — ALL must succeed before any write
                self.on_log("[safety] setting existing data aside…")
                try:
                    for root in flat_roots:
                        if root.exists():
                            aside = self._aside_path(root, ts)
                            self.on_log(f"[safety] {root.name} -> {aside.name}")
                            root.rename(aside)
                    for target in named_targets:
                        if target.exists():
                            aside = self._aside_path(target, ts)
                            self.on_log(f"[safety] {target.name} -> {aside.name}")
                            target.rename(aside)
                    for arc, rel, root in core_plan:
                        target = root / rel
                        if target.is_file():
                            aside = self._aside_path(target, ts)
                            self.on_log(f"[safety] {target.name} -> {aside.name}")
                            target.rename(aside)
                except OSError as e:
                    raise RestoreAborted(
                        f"could not preserve existing data before restore "
                        f"({e}) — restore aborted, nothing was written") from e

                # -- phase 4: extract
                done = 0
                for member, rel, root in plan + core_plan:
                    if self.cancel_check():
                        raise Cancelled
                    dest = self._contained(root, rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, dest.open("wb") as out:
                        while True:
                            chunk = src.read(CHUNK)
                            if not chunk:
                                break
                            out.write(chunk)
                            done += len(chunk)
                            self.on_progress("restore", done, total)
                    try:                                   # keep source mtimes
                        t = time.mktime(zf.getinfo(member).date_time + (0, 0, -1))
                        os.utime(dest, (t, t))
                    except (OSError, OverflowError, ValueError):
                        pass
        except Cancelled:
            return False, ("Restore cancelled after verification — files "
                           "already restored were left in place; originals "
                           f"are preserved as *.pre_restore_{ts}.")
        except (ArchiveError, RestoreAborted) as e:
            return False, f"Restore refused: {e}"
        except Exception as e:
            return False, f"Restore failed: {e}"

        return True, (f"Restore complete: {len(plan) + len(core_plan)} files, "
                      f"{human_bytes(done)}. Previous data (if any) kept as "
                      f"*.pre_restore_{ts}.")


# =====================================================================
# [3] CLI scan mode
# =====================================================================


def cli_scan() -> int:
    out = []
    for st in scan_all(on_log=lambda m: print(m, file=sys.stderr)):
        out.append({
            "id": st.spec.bid,
            "name": st.spec.name,
            "family": st.spec.family,
            "root": str(st.spec.root),
            "running": st.proc.running,
            "process_check_ok": st.proc.ok,
            "core_files": st.core_files,
            "profiles": [{"folder": p.folder, "display": p.display,
                          "files": p.files, "bytes": p.bytes}
                         for p in st.profiles],
        })
    print(json.dumps(out, indent=2))
    return 0


# =====================================================================
# [4] Qt adapters + GUI (optional at import time — tests need stdlib only)
# =====================================================================

try:
    from PySide6.QtCore import Qt, QThread, Signal, Slot
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (
        QApplication, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
        QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
        QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
    )
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False

C_OBSIDIAN = "#0b0f14"
C_STEEL = "#232b35"
C_TEAL = "#2fd6c3"
C_PHOSPHOR = "#4be08a"
C_AMBER = "#ffb454"
C_RED = "#ff5c66"
C_TEXT = "#c9d4de"
C_DIM = "#6b7684"

STYLESHEET = f"""
* {{ font-family: "JetBrains Mono", "Consolas", monospace; font-size: 12px; }}
QMainWindow, QWidget {{ background: {C_OBSIDIAN}; color: {C_TEXT}; }}
QLabel#hdrTitle {{ color: {C_TEAL}; font-size: 15px; font-weight: bold;
                   letter-spacing: 2px; }}
QLabel#hdrVer {{ color: {C_DIM}; }}
QLabel#warnBanner {{ background: #1a1408; color: {C_AMBER};
                     border: 1px solid {C_AMBER}; padding: 6px 10px; }}
QLabel#statusOk   {{ color: {C_PHOSPHOR}; }}
QLabel#statusWarn {{ color: {C_AMBER}; }}
QLabel#statusErr  {{ color: {C_RED}; }}
QPushButton {{ background: {C_STEEL}; color: {C_TEXT};
               border: 1px solid #2f3a47; border-radius: 0px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {C_TEAL}; color: {C_TEAL}; }}
QPushButton:disabled {{ color: {C_DIM}; border-color: {C_STEEL}; }}
QPushButton#primary {{ background: #0f2b27; border: 1px solid {C_TEAL};
                       color: {C_TEAL}; font-weight: bold; }}
QPushButton#danger {{ background: #2b0f12; border: 1px solid {C_RED};
                      color: {C_RED}; }}
QLineEdit, QPlainTextEdit {{ background: #10161d; color: {C_TEXT};
    border: 1px solid #2f3a47; border-radius: 0px; padding: 4px 6px;
    selection-background-color: {C_TEAL}; selection-color: {C_OBSIDIAN}; }}
QTreeWidget {{ background: #10161d; color: {C_TEXT}; border: 1px solid #2f3a47;
               alternate-background-color: #131b23; }}
QTreeWidget::item {{ padding: 3px 2px; }}
QTreeWidget::item:selected {{ background: #17323c; color: {C_TEAL}; }}
QHeaderView::section {{ background: {C_STEEL}; color: {C_DIM}; border: 0px;
    border-right: 1px solid {C_OBSIDIAN}; padding: 4px 6px; }}
QTabWidget::pane {{ border: 1px solid #2f3a47; top: -1px; }}
QTabBar::tab {{ background: {C_STEEL}; color: {C_DIM}; padding: 6px 18px;
    border: 1px solid #2f3a47; border-bottom: 0px; border-radius: 0px; }}
QTabBar::tab:selected {{ background: {C_OBSIDIAN}; color: {C_TEAL}; }}
QProgressBar {{ background: #10161d; border: 1px solid #2f3a47;
    border-radius: 0px; text-align: center; color: {C_TEXT}; height: 16px; }}
QProgressBar::chunk {{ background: {C_TEAL}; }}
"""

if QT_AVAILABLE:

    class ScanWorker(QThread):
        result = Signal(list)
        log = Signal(str)

        def run(self):
            t0 = time.time()
            states = scan_all(with_sizes=True, on_log=self.log.emit)
            self.log.emit(f"[scan] {len(states)} browser(s) found in "
                          f"{time.time() - t0:.1f}s")
            self.result.emit(states)

    class EngineWorker(QThread):
        """Thin Qt adapter over a stdlib engine."""
        progress = Signal(str, int, int)
        log = Signal(str)
        finished_ok = Signal(bool, str)

        def __init__(self, engine_cls, *args, parent=None):
            super().__init__(parent)
            self._cancel = False
            self.engine = engine_cls(
                *args,
                on_log=self.log.emit,
                on_progress=self.progress.emit,
                cancel_check=lambda: self._cancel)

        def cancel(self):
            self._cancel = True

        def run(self):
            ok, msg = self.engine.run()
            self.finished_ok.emit(ok, msg)

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"{APP_NAME} v{APP_VERSION} — 7h3v01d")
            self.resize(980, 680)
            self._worker_refs: set[QThread] = set()
            self._states: list[BrowserState] = []
            self._manifest: dict | None = None
            self._archive_path: Path | None = None
            self._restore_targets: dict[str, Path] = {}
            self._specs = spec_by_id()

            central = QWidget()
            self.setCentralWidget(central)
            v = QVBoxLayout(central)
            v.setContentsMargins(10, 8, 10, 8)
            v.setSpacing(8)

            hdr = QHBoxLayout()
            t = QLabel("PROFILE PORTER")
            t.setObjectName("hdrTitle")
            ver = QLabel(f"v{APP_VERSION}  ::  browser profile migration")
            ver.setObjectName("hdrVer")
            hdr.addWidget(t)
            hdr.addWidget(ver)
            hdr.addStretch(1)
            self.status_chip = QLabel("READY")
            self.status_chip.setObjectName("statusOk")
            hdr.addWidget(self.status_chip)
            v.addLayout(hdr)

            warn = QLabel(
                "NOTE: Chromium-family (Chrome/Edge/Brave/Vivaldi/Opera) saved "
                "passwords, authentication cookies and some protected payment "
                "data are encrypted with Windows-bound credentials — a copied "
                "profile should not be relied on to restore those secrets on "
                "another PC. Use browser sync or password export/import. "
                "Bookmarks, history, extensions and preferences are included, "
                "though the browser may rebuild or re-sync some data on first "
                "launch. Firefox profiles are substantially more portable, "
                "including saved logins.")
            warn.setObjectName("warnBanner")
            warn.setWordWrap(True)
            v.addWidget(warn)

            self.tabs = QTabWidget()
            v.addWidget(self.tabs, 1)
            self._build_backup_tab()
            self._build_restore_tab()
            self._build_log_tab()

            foot = QHBoxLayout()
            self.progress = QProgressBar()
            self.progress.setValue(0)
            foot.addWidget(self.progress, 1)
            self.btn_cancel = QPushButton("CANCEL")
            self.btn_cancel.setObjectName("danger")
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.clicked.connect(self._cancel_active)
            foot.addWidget(self.btn_cancel)
            v.addLayout(foot)

            self._active_worker: QThread | None = None
            self._log(f"{APP_NAME} v{APP_VERSION} — "
                      f"{platform.system()} {platform.release()}")
            if platform.system() != "Windows":
                self._log("[warn] non-Windows host: detection paths are "
                          "Windows-specific.")
            self.rescan()

        # -- tabs ---------------------------------------------------------

        def _build_backup_tab(self):
            w = QWidget()
            lay = QVBoxLayout(w)
            row = QHBoxLayout()
            self.btn_rescan = QPushButton("RESCAN")
            self.btn_rescan.clicked.connect(self.rescan)
            row.addWidget(self.btn_rescan)
            row.addStretch(1)
            lay.addLayout(row)

            self.tree_backup = QTreeWidget()
            self.tree_backup.setHeaderLabels(
                ["Browser / Profile", "Size", "Files", "Path"])
            self.tree_backup.setAlternatingRowColors(True)
            self.tree_backup.header().setSectionResizeMode(0, QHeaderView.Stretch)
            lay.addWidget(self.tree_backup, 1)

            row2 = QHBoxLayout()
            self.ed_dest = QLineEdit(str(
                Path.home() / "Desktop" /
                f"browser_profiles_{datetime.now():%Y%m%d}.zip"))
            row2.addWidget(QLabel("Archive:"))
            row2.addWidget(self.ed_dest, 1)
            b = QPushButton("BROWSE…")
            b.clicked.connect(self._pick_dest)
            row2.addWidget(b)
            self.btn_backup = QPushButton("CREATE BACKUP")
            self.btn_backup.setObjectName("primary")
            self.btn_backup.clicked.connect(self.start_backup)
            row2.addWidget(self.btn_backup)
            lay.addLayout(row2)
            self.tabs.addTab(w, "BACKUP")

        def _build_restore_tab(self):
            w = QWidget()
            lay = QVBoxLayout(w)
            row = QHBoxLayout()
            b = QPushButton("OPEN ARCHIVE…")
            b.clicked.connect(self._open_archive)
            row.addWidget(b)
            self.lbl_manifest = QLabel("no archive loaded")
            self.lbl_manifest.setObjectName("hdrVer")
            row.addWidget(self.lbl_manifest, 1)
            lay.addLayout(row)

            self.tree_restore = QTreeWidget()
            self.tree_restore.setHeaderLabels(
                ["Browser / Profile", "Size", "Files", "Restore target"])
            self.tree_restore.setAlternatingRowColors(True)
            self.tree_restore.header().setSectionResizeMode(0, QHeaderView.Stretch)
            self.tree_restore.currentItemChanged.connect(self._restore_sel_changed)
            lay.addWidget(self.tree_restore, 1)

            row2 = QHBoxLayout()
            row2.addWidget(QLabel("Target root (selected browser):"))
            self.ed_target = QLineEdit()
            self.ed_target.setEnabled(False)
            self.ed_target.editingFinished.connect(self._target_edited)
            row2.addWidget(self.ed_target, 1)
            self.btn_restore = QPushButton("VERIFY + RESTORE")
            self.btn_restore.setObjectName("primary")
            self.btn_restore.clicked.connect(self.start_restore)
            self.btn_restore.setEnabled(False)
            row2.addWidget(self.btn_restore)
            lay.addLayout(row2)
            self.tabs.addTab(w, "RESTORE")

        def _build_log_tab(self):
            self.log_view = QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setFont(QFont("JetBrains Mono", 10))
            self.tabs.addTab(self.log_view, "LOG")

        # -- helpers ------------------------------------------------------

        @Slot(str)
        def _log(self, msg: str):
            self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {msg}")

        def _set_status(self, text: str, kind: str = "ok"):
            self.status_chip.setText(text)
            self.status_chip.setObjectName(
                {"ok": "statusOk", "warn": "statusWarn", "err": "statusErr"}[kind])
            self.status_chip.style().unpolish(self.status_chip)
            self.status_chip.style().polish(self.status_chip)

        def _busy(self, on: bool):
            for b in (self.btn_backup, self.btn_rescan, self.btn_restore):
                b.setEnabled(not on)
            self.btn_cancel.setEnabled(on)
            if not on:
                self._active_worker = None
                if self._manifest is None:
                    self.btn_restore.setEnabled(False)

        def _track(self, worker: QThread):
            self._worker_refs.add(worker)
            worker.finished.connect(lambda w=worker: self._worker_refs.discard(w))

        def _cancel_active(self):
            w = self._active_worker
            if w is not None and hasattr(w, "cancel"):
                w.cancel()
                self._set_status("CANCELLING…", "warn")

        @Slot(str, int, int)
        def _on_progress(self, phase: str, done: int, total: int):
            label = {"verify": "VERIFYING ARCHIVE…", "backup": "BACKING UP…",
                     "restore": "RESTORING…"}.get(phase, phase.upper())
            if self.status_chip.text() != label:
                self._set_status(label, "warn")
            self.progress.setMaximum(max(total, 1))
            self.progress.setValue(min(done, total))

        def _confirm_proc_check_failure(self, names: list[str]) -> bool:
            return QMessageBox.warning(
                self, "Process check unavailable",
                "Could not verify whether these browsers are running:\n\n"
                + "\n".join(names) +
                "\n\nMake absolutely sure they are closed, then continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) == QMessageBox.Yes

        # -- scan / backup --------------------------------------------------

        def rescan(self):
            self._set_status("SCANNING…", "warn")
            self.btn_rescan.setEnabled(False)
            w = ScanWorker(self)
            w.log.connect(self._log)
            w.result.connect(self._scan_done)
            self._track(w)
            w.start()

        @Slot(list)
        def _scan_done(self, states: list):
            self._states = states
            self.tree_backup.clear()
            for st in states:
                suffix = ""
                if st.proc.running:
                    suffix = "   [RUNNING — close it first]"
                elif not st.proc.ok:
                    suffix = "   [process check unavailable]"
                top = QTreeWidgetItem([st.spec.name + suffix,
                                       human_bytes(st.total_bytes), "",
                                       str(st.spec.root)])
                top.setFlags(top.flags() | Qt.ItemIsAutoTristate
                             | Qt.ItemIsUserCheckable)
                top.setCheckState(0, Qt.Unchecked)
                top.setData(0, Qt.UserRole, st)
                if st.proc.running:
                    top.setForeground(0, Qt.red)
                for p in st.profiles:
                    it = QTreeWidgetItem([p.display, human_bytes(p.bytes),
                                          f"{p.files:,}", str(p.path)])
                    it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                    it.setCheckState(0, Qt.Unchecked)
                    it.setData(0, Qt.UserRole, p)
                    top.addChild(it)
                self.tree_backup.addTopLevelItem(top)
                top.setExpanded(True)
            self.btn_rescan.setEnabled(True)
            self._set_status("READY")
            self._log(f"[scan] {sum(len(s.profiles) for s in states)} "
                      f"profile(s) listed")

        def _pick_dest(self):
            path, _ = QFileDialog.getSaveFileName(
                self, "Backup archive", self.ed_dest.text(),
                "ZIP archive (*.zip)")
            if path:
                self.ed_dest.setText(path)

        def start_backup(self):
            jobs: list[tuple[BrowserState, list[ProfileInfo]]] = []
            blocked: list[str] = []
            check_failed: list[str] = []
            snap = tasklist_snapshot()
            for i in range(self.tree_backup.topLevelItemCount()):
                top = self.tree_backup.topLevelItem(i)
                st: BrowserState = top.data(0, Qt.UserRole)
                chosen = [top.child(j).data(0, Qt.UserRole)
                          for j in range(top.childCount())
                          if top.child(j).checkState(0) == Qt.Checked]
                if not chosen:
                    continue
                proc = check_processes(st.spec.processes, snap)
                if not proc.ok:
                    check_failed.append(st.spec.name)
                elif proc.running:
                    blocked.append(f"{st.spec.name} ({', '.join(proc.running)})")
                    continue
                jobs.append((st, chosen))

            if check_failed and not self._confirm_proc_check_failure(check_failed):
                return
            if blocked:
                if not jobs:
                    QMessageBox.warning(
                        self, "Browser running",
                        "Close these browsers first, then retry:\n\n"
                        + "\n".join(blocked))
                    return
                going = ", ".join(sorted({s.spec.name for s, _ in jobs}))
                if QMessageBox.question(
                        self, "Some browsers skipped",
                        "These were SKIPPED because they are running:\n\n"
                        + "\n".join(blocked)
                        + f"\n\nContinue with {going} only?") != QMessageBox.Yes:
                    return
            if not jobs:
                QMessageBox.information(self, "Nothing selected",
                                        "Tick at least one profile to back up.")
                return

            dest = Path(self.ed_dest.text()).expanduser()
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                QMessageBox.warning(self, "Destination unavailable",
                                    f"Cannot create {dest.parent}:\n{e}")
                return
            if dest.exists():
                if QMessageBox.question(
                        self, "Overwrite?",
                        f"{dest.name} exists. Overwrite on success?\n\n"
                        "(The existing archive is kept if the new backup "
                        "fails.)") != QMessageBox.Yes:
                    return

            self._set_status("BACKING UP…", "warn")
            self._busy(True)
            self.progress.setValue(0)
            w = EngineWorker(BackupEngine, jobs, dest, parent=self)
            w.log.connect(self._log)
            w.progress.connect(self._on_progress)
            w.finished_ok.connect(self._backup_done)
            self._active_worker = w
            self._track(w)
            w.start()

        @Slot(bool, str)
        def _backup_done(self, ok: bool, msg: str):
            self._log(("[ok] " if ok else "[err] ") + msg)
            self._set_status("DONE" if ok else "FAILED", "ok" if ok else "err")
            self._busy(False)
            (QMessageBox.information if ok else QMessageBox.warning)(
                self, "Backup", msg)

        # -- restore --------------------------------------------------------

        def _open_archive(self):
            path, _ = QFileDialog.getOpenFileName(
                self, "Open backup archive", str(Path.home()),
                "ZIP archive (*.zip)")
            if not path:
                return
            try:
                with zipfile.ZipFile(path, "r") as zf:
                    manifest = load_and_validate_manifest(zf)
            except (ArchiveError, zipfile.BadZipFile, OSError) as e:
                QMessageBox.warning(self, "Archive rejected", str(e))
                return
            self._manifest = manifest
            self._archive_path = Path(path)
            self._restore_targets = {}
            self.lbl_manifest.setText(
                f"{Path(path).name}  ::  from {manifest.get('host', '?')} "
                f"@ {manifest.get('created_utc', '?')[:19]}  ::  "
                f"{len(manifest['files'])} files (validated — hashes are "
                f"checked before restore)")
            self._log(f"[restore] loaded + structurally validated {path}")

            self.tree_restore.clear()
            for b in manifest["browsers"]:
                spec = self._specs.get(b["id"])
                target = spec.root if spec else Path(b.get("source_root", ""))
                self._restore_targets[b["id"]] = target
                total = sum(p["bytes"] for p in b["profiles"])
                top = QTreeWidgetItem([b["name"], human_bytes(total), "",
                                       str(target)])
                top.setFlags(top.flags() | Qt.ItemIsAutoTristate
                             | Qt.ItemIsUserCheckable)
                top.setCheckState(0, Qt.Unchecked)
                top.setData(0, Qt.UserRole, b)
                for p in b["profiles"]:
                    it = QTreeWidgetItem([p["display"], human_bytes(p["bytes"]),
                                          f"{p['files']:,}", ""])
                    it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                    it.setCheckState(0, Qt.Unchecked)
                    it.setData(0, Qt.UserRole, (b, p))
                    top.addChild(it)
                self.tree_restore.addTopLevelItem(top)
                top.setExpanded(True)
            self.btn_restore.setEnabled(True)

        def _restore_sel_changed(self, cur, _prev):
            if cur is None:
                self.ed_target.setEnabled(False)
                return
            node = cur if cur.parent() is None else cur.parent()
            b = node.data(0, Qt.UserRole)
            self.ed_target.setEnabled(True)
            self.ed_target.setText(str(self._restore_targets.get(b["id"], "")))

        def _target_edited(self):
            cur = self.tree_restore.currentItem()
            if cur is None:
                return
            node = cur if cur.parent() is None else cur.parent()
            b = node.data(0, Qt.UserRole)
            self._restore_targets[b["id"]] = Path(self.ed_target.text()).expanduser()
            node.setText(3, self.ed_target.text())
            self._log(f"[restore] target for {b['name']} -> "
                      f"{self.ed_target.text()}")

        def start_restore(self):
            if self._manifest is None or self._archive_path is None:
                return
            selections: list[tuple[str, str, Path]] = []
            core_map: dict[str, tuple[list[str], Path]] = {}
            blocked: list[str] = []
            check_failed: list[str] = []
            snap = tasklist_snapshot()
            for i in range(self.tree_restore.topLevelItemCount()):
                top = self.tree_restore.topLevelItem(i)
                b = top.data(0, Qt.UserRole)
                chosen = [top.child(j).data(0, Qt.UserRole)[1]
                          for j in range(top.childCount())
                          if top.child(j).checkState(0) == Qt.Checked]
                if not chosen:
                    continue
                spec = self._specs.get(b["id"])
                if spec:
                    proc = check_processes(spec.processes, snap)
                    if not proc.ok:
                        check_failed.append(b["name"])
                    elif proc.running:
                        blocked.append(f"{b['name']} ({', '.join(proc.running)})")
                        continue
                root = self._restore_targets[b["id"]]
                for p in chosen:
                    selections.append((b["id"], p["arc_prefix"], root))
                core_map[b["id"]] = (b.get("core", []), root)

            if check_failed and not self._confirm_proc_check_failure(check_failed):
                return
            if blocked:
                if not selections:
                    QMessageBox.warning(
                        self, "Browser running",
                        "Close these browsers first, then retry:\n\n"
                        + "\n".join(blocked))
                    return
                if QMessageBox.question(
                        self, "Some browsers skipped",
                        "These were SKIPPED because they are running:\n\n"
                        + "\n".join(blocked)
                        + "\n\nContinue with the rest only?") != QMessageBox.Yes:
                    return
            if not selections:
                QMessageBox.information(self, "Nothing selected",
                                        "Tick at least one profile to restore.")
                return
            if QMessageBox.question(
                    self, "Confirm restore",
                    "The archive will be fully verified (SHA-256 + chain) "
                    "first. If verification passes, existing data at the "
                    "target is renamed aside (never deleted) and the selected "
                    "profiles are restored.\n\nProceed?") != QMessageBox.Yes:
                return

            self._set_status("VERIFYING ARCHIVE…", "warn")
            self._busy(True)
            self.progress.setValue(0)
            w = EngineWorker(RestoreEngine, self._archive_path, selections,
                             core_map, parent=self)
            w.log.connect(self._log)
            w.progress.connect(self._on_progress)
            w.finished_ok.connect(self._restore_done)
            self._active_worker = w
            self._track(w)
            w.start()

        @Slot(bool, str)
        def _restore_done(self, ok: bool, msg: str):
            self._log(("[ok] " if ok else "[err] ") + msg)
            self._set_status("DONE" if ok else "FAILED", "ok" if ok else "err")
            self._busy(False)
            (QMessageBox.information if ok else QMessageBox.warning)(
                self, "Restore", msg)


def main() -> int:
    if "--scan" in sys.argv:
        return cli_scan()
    if not QT_AVAILABLE:
        print("PySide6 is not installed. Run: pip install PySide6\n"
              "(--scan mode works without it.)", file=sys.stderr)
        return 1
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
