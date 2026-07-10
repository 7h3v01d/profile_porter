"""Headless round-trip test for Profile Porter (no display needed)."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

tmp = Path(tempfile.mkdtemp(prefix="pp_test_"))
local = tmp / "Local"
roam = tmp / "Roaming"

# --- fake Chrome ---
chrome = local / "Google/Chrome/User Data"
for prof in ("Default", "Profile 1"):
    (chrome / prof / "Extensions/abc").mkdir(parents=True)
    (chrome / prof / "Preferences").write_text('{"profile":{"name":"x"}}', encoding="utf-8")
    (chrome / prof / "Bookmarks").write_text('{"roots":{}}', encoding="utf-8")
    (chrome / prof / "Extensions/abc/manifest.json").write_text("{}", encoding="utf-8")
    (chrome / prof / "Cache").mkdir()
    (chrome / prof / "Cache/junk.bin").write_bytes(b"\x00" * 4096)  # must be excluded
(chrome / "Local State").write_text(json.dumps(
    {"profile": {"info_cache": {"Default": {"name": "Dad"},
                                "Profile 1": {"name": "Spare"}}}}), encoding="utf-8")

# --- fake Firefox ---
ff = roam / "Mozilla/Firefox"
pdir = ff / "Profiles/abcd1234.default-release"
pdir.mkdir(parents=True)
(pdir / "places.sqlite").write_bytes(b"SQLite format 3\x00" + b"\x01" * 100)
(pdir / "logins.json").write_text('{"logins":[]}', encoding="utf-8")
(pdir / "cache2").mkdir()
(pdir / "cache2/big").write_bytes(b"\xff" * 2048)  # excluded
(ff / "profiles.ini").write_text(
    "[Profile0]\nName=default-release\nIsRelative=1\n"
    "Path=Profiles/abcd1234.default-release\nDefault=1\n", encoding="utf-8")

os.environ["LOCALAPPDATA"] = str(local)
os.environ["APPDATA"] = str(roam)

import profile_porter as pp  # noqa: E402  (after env vars set)
from PySide6.QtCore import QCoreApplication  # noqa: E402

app = QCoreApplication(sys.argv)
pp.SPEC_BY_ID = {s.bid: s for s in pp.build_specs()}  # rebuild with test env

states = pp.scan_all()
names = {s.spec.bid for s in states}
assert names == {"chrome", "firefox"}, names
chrome_state = next(s for s in states if s.spec.bid == "chrome")
assert len(chrome_state.profiles) == 2
assert "Local State" in chrome_state.core_files
print("[1/4] discovery OK:", {s.spec.bid: len(s.profiles) for s in states})

# --- backup everything ---
archive = tmp / "backup.zip"
jobs = [(s, list(s.profiles)) for s in states]
bw = pp.BackupWorker(jobs, archive)
results = []
bw.finished_ok.connect(lambda ok, msg: results.append((ok, msg)))
bw.log.connect(lambda m: None)
bw.run()  # synchronous
ok, msg = results[-1]
assert ok, msg
print("[2/4] backup OK:", msg)

import zipfile  # noqa: E402
with zipfile.ZipFile(archive) as zf:
    nl = zf.namelist()
    manifest = json.loads(zf.read("manifest.json"))
assert "chrome/root/Default/Bookmarks" in nl
assert "chrome/root/Local State" in nl
assert "firefox/root/Profiles/abcd1234.default-release/logins.json" in nl
assert not any("Cache" in n or "cache2" in n for n in nl), "cache leaked into archive"
assert manifest["chain_head"] and len(manifest["files"]) == len(
    [n for n in nl if n != "manifest.json"])
print("[3/4] archive contents + manifest chain OK "
      f"({len(manifest['files'])} files)")

# --- restore into a FRESH machine root, with one pre-existing profile ---
new_local = tmp / "NewLocal"
new_chrome = new_local / "Google/Chrome/User Data"
(new_chrome / "Default").mkdir(parents=True)
(new_chrome / "Default/Preferences").write_text("{}", encoding="utf-8")  # gets set aside
new_ff = tmp / "NewRoam/Mozilla/Firefox"

selections, core_map = [], {}
for b in manifest["browsers"]:
    root = new_chrome if b["id"] == "chrome" else new_ff
    for p in b["profiles"]:
        selections.append((b["id"], p["arc_prefix"], root))
    core_map[b["id"]] = (b.get("core", []), root)

rw = pp.RestoreWorker(archive, selections, core_map)
results.clear()
rw.finished_ok.connect(lambda ok, msg: results.append((ok, msg)))
rw.log.connect(lambda m: None)
rw.run()
ok, msg = results[-1]
assert ok, msg

assert (new_chrome / "Default/Bookmarks").is_file()
assert (new_chrome / "Profile 1/Preferences").is_file()
assert (new_chrome / "Local State").is_file()
assert (new_ff / "profiles.ini").is_file()
assert (new_ff / "Profiles/abcd1234.default-release/logins.json").is_file()
aside = [p.name for p in new_chrome.iterdir() if ".pre_restore_" in p.name]
assert aside, "existing Default was not set aside"
print("[4/4] restore OK, safety rename:", aside)

shutil.rmtree(tmp)
print("ALL ROUND-TRIP CHECKS PASSED")
