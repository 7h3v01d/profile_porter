#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Profile Porter v1.0.0
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

Backup  : streams selected profiles into a single ZIP with a chain-hashed
          SHA-256 manifest (manifest.json). Caches are excluded.
Restore : reads the manifest, restores selected profiles to the matching
          locations on the new machine. Existing data is renamed aside
          (never deleted) before anything is written.

CLI     : `python profile_porter.py --scan` prints detection JSON, no GUI.

IMPORTANT LIMITATION (by Windows design, not this tool):
  Chromium-family browsers (Chrome/Edge/Brave/Vivaldi/Opera) encrypt saved
  passwords and cookies with Windows DPAPI, keyed to the ORIGINAL machine +
  user account. Those secrets cannot decrypt on a new PC. Bookmarks, history,
  extensions, settings, open tabs, autofill (non-payment) all transfer fine.
  For passwords: use the browser's built-in sync, or export passwords to CSV
  on the old PC and import on the new one.
  Firefox profiles transfer COMPLETELY, passwords included.
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

APP_NAME = "Profile Porter"
APP_VERSION = "1.0.0"
MANIFEST_NAME = "manifest.json"
CHUNK = 1024 * 1024  # 1 MiB streaming chunks

# --- exclusions ------------------------------------------------------------

CHROMIUM_SKIP_DIRS = {
    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
    "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "CacheStorage",
    "ScriptCache", "Media Cache", "Crashpad", "Crash Reports",
    "component_crx_cache", "GraphiteDawnCache", "optimization_guide_model_store",
    "BrowserMetrics", "Safe Browsing",
}
FIREFOX_SKIP_DIRS = {
    "cache2", "startupCache", "crashes", "minidumps", "thumbnails",
    "shader-cache", "saved-telemetry-pings",
}
SKIP_FILES = {
    "lockfile", "parent.lock", ".parentlock",
    "SingletonCookie", "SingletonLock", "SingletonSocket",
    "LOCK",  # leveldb lock stubs — regenerated
}

# --- browser catalog ---------------------------------------------------------


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


SPEC_BY_ID = {s.bid: s for s in build_specs()}

# --- filesystem helpers ------------------------------------------------------


def iter_files(root: Path, skip_dirs: set[str]):
    """Yield files under root, pruning cache/lock noise."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if fn in SKIP_FILES:
                continue
            yield Path(dirpath) / fn


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


def running_processes(names: list[str]) -> list[str]:
    """Return which of the given executables are currently running (Windows)."""
    if platform.system() != "Windows":
        return []
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.lower()
    except Exception:
        return []
    return [n for n in names if f'"{n.lower()}"' in out]

# --- discovery ---------------------------------------------------------------


@dataclass
class ProfileInfo:
    folder: str           # rel path from browser root ("" for flat/whole-root)
    display: str
    path: Path
    files: int = 0
    bytes: int = 0


@dataclass
class BrowserState:
    spec: BrowserSpec
    profiles: list[ProfileInfo] = field(default_factory=list)
    core_files: list[str] = field(default_factory=list)   # rel paths from root
    running: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(p.bytes for p in self.profiles)


def _chromium_display_names(root: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    ls = root / "Local State"
    try:
        data = json.loads(ls.read_text(encoding="utf-8"))
        cache = data.get("profile", {}).get("info_cache", {})
        for folder, meta in cache.items():
            nm = meta.get("name") or meta.get("gaia_name")
            if nm:
                names[folder] = nm
    except Exception:
        pass
    return names


def discover_browser(spec: BrowserSpec, with_sizes: bool = True) -> BrowserState | None:
    root = spec.root
    if not root.is_dir():
        return None
    state = BrowserState(spec=spec, running=running_processes(spec.processes))

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
            is_rel = cp.get(section, "IsRelative", fallback="1") == "1"
            ppath = (root / rel) if is_rel else Path(rel)
            if not ppath.is_dir():
                continue
            disp = cp.get(section, "Name", fallback=rel)
            folder = rel.replace("\\", "/") if is_rel else None
            if folder is None:
                continue  # absolute-path profiles are out of scope
            state.profiles.append(ProfileInfo(folder, f"{disp}  ({rel})", ppath))
        for core in ("profiles.ini", "installs.ini"):
            if (root / core).is_file():
                state.core_files.append(core)

    if not state.profiles:
        return None

    if with_sizes:
        for p in state.profiles:
            p.files, p.bytes = dir_stats(p.path, spec.skip_dirs)
    return state


def scan_all(with_sizes: bool = True) -> list[BrowserState]:
    found = []
    for spec in build_specs():
        st = discover_browser(spec, with_sizes=with_sizes)
        if st:
            found.append(st)
    return found

# --- CLI scan mode (no GUI) ---------------------------------------------------


def cli_scan() -> int:
    out = []
    for st in scan_all():
        out.append({
            "id": st.spec.bid,
            "name": st.spec.name,
            "family": st.spec.family,
            "root": str(st.spec.root),
            "running": st.running,
            "core_files": st.core_files,
            "profiles": [
                {"folder": p.folder, "display": p.display,
                 "files": p.files, "bytes": p.bytes}
                for p in st.profiles
            ],
        })
    print(json.dumps(out, indent=2))
    return 0


if "--scan" in sys.argv:
    sys.exit(cli_scan())

# --- Qt ----------------------------------------------------------------------

from PySide6.QtCore import Qt, QThread, Signal, Slot          # noqa: E402
from PySide6.QtGui import QFont                                # noqa: E402
from PySide6.QtWidgets import (                                # noqa: E402
    QApplication, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QFrame,
)

# --- dark-industrial theme -----------------------------------------------------

C_OBSIDIAN = "#0b0f14"
C_STEEL = "#232b35"
C_TEAL = "#2fd6c3"
C_PHOSPHOR = "#4be08a"
C_AMBER = "#ffb454"
C_RED = "#ff5c66"
C_TEXT = "#c9d4de"
C_DIM = "#6b7684"

STYLESHEET = f"""
* {{
    font-family: "JetBrains Mono", "Consolas", monospace;
    font-size: 12px;
}}
QMainWindow, QWidget {{
    background: {C_OBSIDIAN};
    color: {C_TEXT};
}}
QLabel#hdrTitle {{
    color: {C_TEAL};
    font-size: 15px;
    font-weight: bold;
    letter-spacing: 2px;
}}
QLabel#hdrVer {{ color: {C_DIM}; }}
QLabel#warnBanner {{
    background: #1a1408;
    color: {C_AMBER};
    border: 1px solid {C_AMBER};
    padding: 6px 10px;
}}
QLabel#statusOk   {{ color: {C_PHOSPHOR}; }}
QLabel#statusWarn {{ color: {C_AMBER}; }}
QLabel#statusErr  {{ color: {C_RED}; }}
QPushButton {{
    background: {C_STEEL};
    color: {C_TEXT};
    border: 1px solid #2f3a47;
    border-radius: 0px;
    padding: 6px 14px;
}}
QPushButton:hover {{ border-color: {C_TEAL}; color: {C_TEAL}; }}
QPushButton:disabled {{ color: {C_DIM}; border-color: {C_STEEL}; }}
QPushButton#primary {{
    background: #0f2b27;
    border: 1px solid {C_TEAL};
    color: {C_TEAL};
    font-weight: bold;
}}
QPushButton#danger {{
    background: #2b0f12;
    border: 1px solid {C_RED};
    color: {C_RED};
}}
QLineEdit, QPlainTextEdit {{
    background: #10161d;
    color: {C_TEXT};
    border: 1px solid #2f3a47;
    border-radius: 0px;
    padding: 4px 6px;
    selection-background-color: {C_TEAL};
    selection-color: {C_OBSIDIAN};
}}
QTreeWidget {{
    background: #10161d;
    color: {C_TEXT};
    border: 1px solid #2f3a47;
    alternate-background-color: #131b23;
}}
QTreeWidget::item {{ padding: 3px 2px; }}
QTreeWidget::item:selected {{ background: #17323c; color: {C_TEAL}; }}
QHeaderView::section {{
    background: {C_STEEL};
    color: {C_DIM};
    border: 0px;
    border-right: 1px solid {C_OBSIDIAN};
    padding: 4px 6px;
}}
QTabWidget::pane {{ border: 1px solid #2f3a47; top: -1px; }}
QTabBar::tab {{
    background: {C_STEEL};
    color: {C_DIM};
    padding: 6px 18px;
    border: 1px solid #2f3a47;
    border-bottom: 0px;
    border-radius: 0px;
}}
QTabBar::tab:selected {{ background: {C_OBSIDIAN}; color: {C_TEAL}; }}
QProgressBar {{
    background: #10161d;
    border: 1px solid #2f3a47;
    border-radius: 0px;
    text-align: center;
    color: {C_TEXT};
    height: 16px;
}}
QProgressBar::chunk {{ background: {C_TEAL}; }}
QFrame#sep {{ background: #2f3a47; max-height: 1px; }}
"""

# --- workers -------------------------------------------------------------------


class ScanWorker(QThread):
    result = Signal(list)
    log = Signal(str)

    def run(self):
        t0 = time.time()
        states = scan_all(with_sizes=True)
        self.log.emit(f"[scan] {len(states)} browser(s) found in {time.time() - t0:.1f}s")
        self.result.emit(states)


class BackupWorker(QThread):
    """Streams selected profiles into a ZIP with a chain-hashed manifest."""
    progress = Signal(int, int)          # done_bytes, total_bytes
    log = Signal(str)
    finished_ok = Signal(bool, str)      # ok, message / archive path

    def __init__(self, jobs: list[tuple[BrowserState, list[ProfileInfo]]],
                 dest: Path, parent=None):
        super().__init__(parent)
        self.jobs = jobs
        self.dest = dest
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _add_file(self, zf: zipfile.ZipFile, src: Path, arc: str) -> tuple[str, int]:
        h = hashlib.sha256()
        size = 0
        zi = zipfile.ZipInfo(arc, date_time=time.localtime(time.time())[:6])
        zi.compress_type = zipfile.ZIP_DEFLATED
        with src.open("rb") as f, zf.open(zi, "w") as dst:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                dst.write(chunk)
                size += len(chunk)
        return h.hexdigest(), size

    def run(self):
        total = sum(p.bytes for _, plist in self.jobs for p in plist)
        done = 0
        chain = "0" * 64
        files_manifest: list[dict] = []
        browsers_manifest: list[dict] = []
        skipped: list[str] = []

        self.log.emit(f"[backup] target: {self.dest}")
        try:
            with zipfile.ZipFile(self.dest, "w", zipfile.ZIP_DEFLATED,
                                 allowZip64=True) as zf:
                for state, plist in self.jobs:
                    spec = state.spec
                    b_entry = {
                        "id": spec.bid, "name": spec.name, "family": spec.family,
                        "source_root": str(spec.root),
                        "core": [], "profiles": [],
                    }
                    # core files (Local State / profiles.ini etc.)
                    for rel in state.core_files:
                        src = spec.root / rel
                        arc = f"{spec.bid}/root/{PurePosixPath(rel)}"
                        try:
                            digest, size = self._add_file(zf, src, arc)
                        except OSError as e:
                            skipped.append(f"{src} ({e})")
                            continue
                        chain = hashlib.sha256((chain + digest).encode()).hexdigest()
                        files_manifest.append({"arc": arc, "sha256": digest,
                                               "bytes": size, "chain": chain})
                        b_entry["core"].append(arc)

                    for prof in plist:
                        if self._cancel:
                            raise InterruptedError
                        self.log.emit(f"[backup] {spec.name} :: {prof.display}")
                        pf, pb = 0, 0
                        for src in iter_files(prof.path, spec.skip_dirs):
                            if self._cancel:
                                raise InterruptedError
                            rel = src.relative_to(spec.root).as_posix()
                            arc = f"{spec.bid}/root/{rel}"
                            try:
                                digest, size = self._add_file(zf, src, arc)
                            except OSError as e:
                                skipped.append(f"{src} ({e})")
                                continue
                            chain = hashlib.sha256((chain + digest).encode()).hexdigest()
                            files_manifest.append({"arc": arc, "sha256": digest,
                                                   "bytes": size, "chain": chain})
                            pf += 1
                            pb += size
                            done += size
                            self.progress.emit(done, total)
                        b_entry["profiles"].append({
                            "folder": prof.folder,
                            "display": prof.display,
                            "arc_prefix": f"{spec.bid}/root/{prof.folder}".rstrip("/"),
                            "files": pf, "bytes": pb,
                        })
                    browsers_manifest.append(b_entry)

                manifest = {
                    "tool": APP_NAME, "version": APP_VERSION,
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                    "host": platform.node(),
                    "user": os.environ.get("USERNAME", ""),
                    "chain_head": chain,
                    "skipped": skipped,
                    "browsers": browsers_manifest,
                    "files": files_manifest,
                }
                zf.writestr(MANIFEST_NAME,
                            json.dumps(manifest, indent=1))
        except InterruptedError:
            try:
                self.dest.unlink(missing_ok=True)
            except OSError:
                pass
            self.finished_ok.emit(False, "Backup cancelled — partial archive removed.")
            return
        except Exception as e:
            self.finished_ok.emit(False, f"Backup failed: {e}")
            return

        msg = (f"Backup complete: {len(files_manifest)} files, "
               f"{human_bytes(done)} -> {self.dest}")
        if skipped:
            msg += f"  ({len(skipped)} unreadable files skipped — see log)"
            for s in skipped[:50]:
                self.log.emit(f"[skipped] {s}")
        self.finished_ok.emit(True, msg)


class RestoreWorker(QThread):
    """Restores selected arc-prefixes from an archive. Never deletes —
    existing targets are renamed aside first."""
    progress = Signal(int, int)
    log = Signal(str)
    finished_ok = Signal(bool, str)

    def __init__(self, archive: Path,
                 selections: list[tuple[str, str, Path]],   # (bid, arc_prefix, target_root)
                 core_map: dict[str, tuple[list[str], Path]],  # bid -> (core arcs, root)
                 parent=None):
        super().__init__(parent)
        self.archive = archive
        self.selections = selections
        self.core_map = core_map
        self._cancel = False

    def cancel(self):
        self._cancel = True

    @staticmethod
    def _safe_dest(root: Path, rel: str) -> Path:
        dest = (root / rel).resolve()
        if not str(dest).startswith(str(root.resolve())):
            raise ValueError(f"path traversal blocked: {rel}")
        return dest

    def run(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            with zipfile.ZipFile(self.archive, "r") as zf:
                names = zf.namelist()

                # figure out which members we restore
                plan: list[tuple[str, str, Path]] = []   # (member, rel, root)
                for bid, prefix, root in self.selections:
                    base = f"{bid}/root/"
                    hit = [n for n in names
                           if n.startswith(prefix + "/") or n == prefix]
                    for n in hit:
                        plan.append((n, n[len(base):], root))
                for bid, (arcs, root) in self.core_map.items():
                    base = f"{bid}/root/"
                    for arc in arcs:
                        if arc in names:
                            plan.append((arc, arc[len(base):], root))

                if not plan:
                    self.finished_ok.emit(False, "Nothing matched in archive.")
                    return

                total = sum(zf.getinfo(m).file_size for m, _, _ in plan)
                done = 0

                # safety renames: profile dirs + core files that already exist
                renamed = set()
                for bid, prefix, root in self.selections:
                    folder = prefix[len(f"{bid}/root/"):] if prefix != f"{bid}/root" else ""
                    if not folder:
                        continue
                    target = root / folder
                    if target.exists() and target not in renamed:
                        aside = target.with_name(target.name + f".pre_restore_{ts}")
                        self.log.emit(f"[safety] existing -> {aside.name}")
                        target.rename(aside)
                        renamed.add(target)
                for bid, (arcs, root) in self.core_map.items():
                    for arc in arcs:
                        rel = arc[len(f'{bid}/root/'):]
                        target = root / rel
                        if target.is_file():
                            aside = target.with_name(target.name + f".pre_restore_{ts}")
                            try:
                                aside.unlink(missing_ok=True)
                                target.rename(aside)
                                self.log.emit(f"[safety] existing -> {aside.name}")
                            except OSError as e:
                                self.log.emit(f"[warn] could not set aside {target}: {e}")

                for member, rel, root in plan:
                    if self._cancel:
                        raise InterruptedError
                    dest = self._safe_dest(root, rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, dest.open("wb") as out:
                        while True:
                            chunk = src.read(CHUNK)
                            if not chunk:
                                break
                            out.write(chunk)
                            done += len(chunk)
                            self.progress.emit(done, total)
        except InterruptedError:
            self.finished_ok.emit(
                False,
                "Restore cancelled — restored files were left in place; "
                f"originals are preserved as *.pre_restore_{ts}.")
            return
        except Exception as e:
            self.finished_ok.emit(False, f"Restore failed: {e}")
            return

        self.finished_ok.emit(
            True,
            f"Restore complete: {len(plan)} files, {human_bytes(done)}. "
            f"Previous data (if any) kept as *.pre_restore_{ts}.")


# --- main window ----------------------------------------------------------------


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

        central = QWidget()
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(8)

        # header
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
            "NOTE: Chrome/Edge/Brave/Vivaldi/Opera passwords + cookies are locked to the "
            "old PC by Windows (DPAPI) and will NOT transfer. Use browser sync or export "
            "passwords to CSV before migrating. Everything else (bookmarks, history, "
            "extensions, settings) transfers. Firefox transfers completely.")
        warn.setObjectName("warnBanner")
        warn.setWordWrap(True)
        v.addWidget(warn)

        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)
        self._build_backup_tab()
        self._build_restore_tab()
        self._build_log_tab()

        # footer: progress + cancel
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
        self._log(f"{APP_NAME} v{APP_VERSION} — {platform.system()} {platform.release()}")
        if platform.system() != "Windows":
            self._log("[warn] non-Windows host: detection paths are Windows-specific.")
        self.rescan()

    # -- tabs --------------------------------------------------------------

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
        self.tree_backup.setHeaderLabels(["Browser / Profile", "Size", "Files", "Path"])
        self.tree_backup.setAlternatingRowColors(True)
        self.tree_backup.header().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self.tree_backup, 1)

        row2 = QHBoxLayout()
        self.ed_dest = QLineEdit(str(Path.home() / "Desktop" /
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
        self.tree_restore.setHeaderLabels(["Browser / Profile", "Size", "Files",
                                           "Restore target"])
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
        self.btn_restore = QPushButton("RESTORE SELECTED")
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

    # -- helpers -----------------------------------------------------------

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

    @Slot(int, int)
    def _on_progress(self, done: int, total: int):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(min(done, total))

    # -- scan / backup -------------------------------------------------------

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
            top = QTreeWidgetItem([
                st.spec.name + ("   [RUNNING — close it first]" if st.running else ""),
                human_bytes(st.total_bytes), "", str(st.spec.root)])
            top.setFlags(top.flags() | Qt.ItemIsAutoTristate | Qt.ItemIsUserCheckable)
            top.setCheckState(0, Qt.Unchecked)
            top.setData(0, Qt.UserRole, st)
            if st.running:
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
        self._log(f"[scan] {sum(len(s.profiles) for s in states)} profile(s) listed")

    def _pick_dest(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Backup archive", self.ed_dest.text(), "ZIP archive (*.zip)")
        if path:
            self.ed_dest.setText(path)

    def _collect_backup_jobs(self):
        jobs = []
        blocked = []
        for i in range(self.tree_backup.topLevelItemCount()):
            top = self.tree_backup.topLevelItem(i)
            st: BrowserState = top.data(0, Qt.UserRole)
            chosen = [top.child(j).data(0, Qt.UserRole)
                      for j in range(top.childCount())
                      if top.child(j).checkState(0) == Qt.Checked]
            if not chosen:
                continue
            live = running_processes(st.spec.processes)
            if live:
                blocked.append(f"{st.spec.name} ({', '.join(live)})")
                continue
            jobs.append((st, chosen))
        return jobs, blocked

    def start_backup(self):
        jobs, blocked = self._collect_backup_jobs()
        if blocked:
            QMessageBox.warning(
                self, "Browser running",
                "Close these browsers first, then retry:\n\n" + "\n".join(blocked))
            if not jobs:
                return
        if not jobs:
            QMessageBox.information(self, "Nothing selected",
                                    "Tick at least one profile to back up.")
            return
        dest = Path(self.ed_dest.text()).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if QMessageBox.question(
                    self, "Overwrite?",
                    f"{dest.name} exists. Overwrite?") != QMessageBox.Yes:
                return

        self._set_status("BACKING UP…", "warn")
        self._busy(True)
        self.progress.setValue(0)
        w = BackupWorker(jobs, dest, self)
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

    # -- restore ---------------------------------------------------------------

    def _open_archive(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open backup archive", str(Path.home()), "ZIP archive (*.zip)")
        if not path:
            return
        try:
            with zipfile.ZipFile(path, "r") as zf:
                manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        except Exception as e:
            QMessageBox.warning(self, "Invalid archive",
                                f"Could not read {MANIFEST_NAME}: {e}")
            return
        self._manifest = manifest
        self._archive_path = Path(path)
        self._restore_targets = {}
        self.lbl_manifest.setText(
            f"{Path(path).name}  ::  from {manifest.get('host', '?')} "
            f"@ {manifest.get('created_utc', '?')[:19]}  ::  "
            f"{len(manifest.get('files', []))} files")
        self._log(f"[restore] loaded {path}")

        self.tree_restore.clear()
        for b in manifest.get("browsers", []):
            spec = SPEC_BY_ID.get(b["id"])
            target = spec.root if spec else Path(b.get("source_root", ""))
            self._restore_targets[b["id"]] = target
            total = sum(p["bytes"] for p in b["profiles"])
            top = QTreeWidgetItem([b["name"], human_bytes(total), "", str(target)])
            top.setFlags(top.flags() | Qt.ItemIsAutoTristate | Qt.ItemIsUserCheckable)
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
        self._log(f"[restore] target for {b['name']} -> {self.ed_target.text()}")

    def start_restore(self):
        if self._manifest is None or self._archive_path is None:
            return
        selections: list[tuple[str, str, Path]] = []
        core_map: dict[str, tuple[list[str], Path]] = {}
        blocked = []
        for i in range(self.tree_restore.topLevelItemCount()):
            top = self.tree_restore.topLevelItem(i)
            b = top.data(0, Qt.UserRole)
            chosen = [top.child(j).data(0, Qt.UserRole)[1]
                      for j in range(top.childCount())
                      if top.child(j).checkState(0) == Qt.Checked]
            if not chosen:
                continue
            spec = SPEC_BY_ID.get(b["id"])
            if spec:
                live = running_processes(spec.processes)
                if live:
                    blocked.append(f"{b['name']} ({', '.join(live)})")
                    continue
            root = self._restore_targets[b["id"]]
            for p in chosen:
                selections.append((b["id"], p["arc_prefix"], root))
            core_map[b["id"]] = (b.get("core", []), root)
        if blocked:
            QMessageBox.warning(
                self, "Browser running",
                "Close these browsers first, then retry:\n\n" + "\n".join(blocked))
            if not selections:
                return
        if not selections:
            QMessageBox.information(self, "Nothing selected",
                                    "Tick at least one profile to restore.")
            return
        if QMessageBox.question(
                self, "Confirm restore",
                "Restore the selected profiles?\n\nExisting data at the target "
                "will be renamed aside (never deleted).") != QMessageBox.Yes:
            return

        self._set_status("RESTORING…", "warn")
        self._busy(True)
        self.progress.setValue(0)
        w = RestoreWorker(self._archive_path, selections, core_map, self)
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
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
