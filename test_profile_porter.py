"""Profile Porter v1.0.1 test suite — stdlib + pytest only (no Qt needed).

Covers the round trip plus the restore-safety hardening:
tampered content / manifest / chain, duplicate members, traversal,
flat-profile rename, fatal preservation failure, unknown browser id,
malformed / future-format manifests, installs.ini restore skip,
.partial protection of a previous archive.
"""
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

import profile_porter as pp

# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def no_tasklist(monkeypatch):
    """Never spawn tasklist during tests — it costs ~1s per call on Windows
    and made the suite take minutes. Engines don't care; the GUI paths that
    use it are exercised on real hardware."""
    monkeypatch.setattr(pp, "tasklist_snapshot", lambda: ("", True))


def make_fake_machine(tmp: Path) -> tuple[Path, Path]:
    local, roam = tmp / "Local", tmp / "Roaming"

    chrome = local / "Google/Chrome/User Data"
    for prof in ("Default", "Profile 1"):
        (chrome / prof / "Extensions/abc").mkdir(parents=True)
        (chrome / prof / "Preferences").write_text('{"profile":{}}', encoding="utf-8")
        (chrome / prof / "Bookmarks").write_text('{"roots":{}}', encoding="utf-8")
        (chrome / prof / "Extensions/abc/manifest.json").write_text("{}", encoding="utf-8")
        (chrome / prof / "Cache").mkdir()
        (chrome / prof / "Cache/junk.bin").write_bytes(b"\x00" * 4096)
        (chrome / prof / "code cache").mkdir()          # lowercase — casefold test
        (chrome / prof / "code cache/x.bin").write_bytes(b"\x01" * 512)
    (chrome / "Local State").write_text(json.dumps(
        {"profile": {"info_cache": {"Default": {"name": "Dad"},
                                    "Profile 1": {"name": "Spare"}}}}),
        encoding="utf-8")

    opera = roam / "Opera Software/Opera Stable"
    opera.mkdir(parents=True)
    (opera / "Preferences").write_text("{}", encoding="utf-8")
    (opera / "Bookmarks").write_text('{"roots":{}}', encoding="utf-8")

    ff = roam / "Mozilla/Firefox"
    pdir = ff / "Profiles/abcd1234.default-release"
    pdir.mkdir(parents=True)
    (pdir / "places.sqlite").write_bytes(b"SQLite format 3\x00" + b"\x01" * 100)
    (pdir / "logins.json").write_text('{"logins":[]}', encoding="utf-8")
    (pdir / "cache2").mkdir()
    (pdir / "cache2/big").write_bytes(b"\xff" * 2048)
    (ff / "profiles.ini").write_text(
        "[Profile0]\nName=default-release\nIsRelative=1\n"
        "Path=Profiles/abcd1234.default-release\nDefault=1\n", encoding="utf-8")
    (ff / "installs.ini").write_text(
        "[ABCDEF0123456789]\nDefault=Profiles/abcd1234.default-release\n",
        encoding="utf-8")
    return local, roam


@pytest.fixture
def machine(tmp_path, monkeypatch):
    local, roam = make_fake_machine(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roam))
    return tmp_path


def do_backup(tmp: Path) -> tuple[Path, dict]:
    states = pp.scan_all()
    archive = tmp / "backup.zip"
    ok, msg = pp.BackupEngine([(s, list(s.profiles)) for s in states],
                              archive).run()
    assert ok, msg
    with zipfile.ZipFile(archive) as zf:
        manifest = pp.load_and_validate_manifest(zf)
    return archive, manifest


def selections_for(manifest: dict, roots: dict[str, Path]):
    selections, core_map = [], {}
    for b in manifest["browsers"]:
        root = roots[b["id"]]
        for p in b["profiles"]:
            selections.append((b["id"], p["arc_prefix"], root))
        core_map[b["id"]] = (b.get("core", []), root)
    return selections, core_map


def new_roots(tmp: Path) -> dict[str, Path]:
    return {
        "chrome": tmp / "New/Chrome/User Data",
        "opera": tmp / "New/Opera Stable",
        "firefox": tmp / "New/Firefox",
    }


def rewrite_zip(src: Path, dst: Path, mutate):
    """Copy a zip, letting `mutate(name, data) -> (name, data) | None | list`
    tamper with members."""
    with zipfile.ZipFile(src) as zin, \
            zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            out = mutate(info.filename, zin.read(info.filename))
            if out is None:
                continue
            items = out if isinstance(out, list) else [out]
            for name, data in items:
                zout.writestr(name, data)


def edit_manifest(src: Path, dst: Path, editor):
    def mutate(name, data):
        if name == pp.MANIFEST_NAME:
            m = json.loads(data)
            editor(m)
            return name, json.dumps(m)
        return name, data
    rewrite_zip(src, dst, mutate)


# ---------------------------------------------------------------- discovery


def test_discovery(machine):
    states = {s.spec.bid: s for s in pp.scan_all()}
    assert set(states) == {"chrome", "opera", "firefox"}
    assert len(states["chrome"].profiles) == 2
    assert "Local State" in states["chrome"].core_files
    assert states["opera"].profiles[0].folder == ""      # flat
    assert "installs.ini" in states["firefox"].core_files


# ---------------------------------------------------------------- round trip


def test_roundtrip(machine):
    archive, manifest = do_backup(machine)
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        pp.verify_archive_hashes(zf, manifest)   # chain actually verified

    assert "chrome/root/Default/Bookmarks" in names
    assert "opera/root/Bookmarks" in names
    assert "firefox/root/Profiles/abcd1234.default-release/logins.json" in names
    low = [n.lower() for n in names]
    assert not any("cache" in n for n in low), "cache leaked (casefold)"

    roots = new_roots(machine)
    # pre-existing data at every target, to exercise the safety renames
    (roots["chrome"] / "Default").mkdir(parents=True)
    (roots["chrome"] / "Default/Preferences").write_text("OLD", encoding="utf-8")
    (roots["chrome"] / "Local State").parent.mkdir(parents=True, exist_ok=True)
    (roots["chrome"] / "Local State").write_text("OLD", encoding="utf-8")
    roots["opera"].mkdir(parents=True)
    (roots["opera"] / "leftover.txt").write_text("OLD", encoding="utf-8")

    selections, core_map = selections_for(manifest, roots)
    ok, msg = pp.RestoreEngine(archive, selections, core_map).run()
    assert ok, msg

    assert (roots["chrome"] / "Default/Bookmarks").is_file()
    assert (roots["chrome"] / "Profile 1/Preferences").is_file()
    assert (roots["chrome"] / "Local State").read_text(encoding="utf-8") != "OLD"
    assert (roots["opera"] / "Bookmarks").is_file()
    assert (roots["firefox"] / "profiles.ini").is_file()
    assert (roots["firefox"] / "Profiles/abcd1234.default-release/logins.json").is_file()

    # named profile + core file set aside
    chrome_aside = [p.name for p in roots["chrome"].iterdir()
                    if ".pre_restore_" in p.name]
    assert any(a.startswith("Default.") for a in chrome_aside)
    assert any(a.startswith("Local State.") for a in chrome_aside)


def test_flat_profile_renamed_aside(machine):
    """Opera (flat) must get a whole-root rename — no hybrid profiles."""
    archive, manifest = do_backup(machine)
    roots = new_roots(machine)
    roots["opera"].mkdir(parents=True)
    (roots["opera"] / "leftover.txt").write_text("OLD", encoding="utf-8")

    selections, core_map = selections_for(manifest, roots)
    ok, msg = pp.RestoreEngine(archive, selections, core_map).run()
    assert ok, msg

    assert not (roots["opera"] / "leftover.txt").exists(), \
        "old file merged into restored flat profile"
    asides = [p for p in roots["opera"].parent.iterdir()
              if ".pre_restore_" in p.name]
    assert asides and (asides[0] / "leftover.txt").is_file()


def test_installs_ini_backed_up_but_not_restored(machine):
    archive, manifest = do_backup(machine)
    with zipfile.ZipFile(archive) as zf:
        assert "firefox/root/installs.ini" in zf.namelist()
    roots = new_roots(machine)
    selections, core_map = selections_for(manifest, roots)
    ok, msg = pp.RestoreEngine(archive, selections, core_map).run()
    assert ok, msg
    assert (roots["firefox"] / "profiles.ini").is_file()
    assert not (roots["firefox"] / "installs.ini").exists()


def test_mtime_preserved(machine):
    src = (Path(os.environ["LOCALAPPDATA"])
           / "Google/Chrome/User Data/Default/Bookmarks")
    old = 946684800.0  # 2000-01-01
    os.utime(src, (old, old))
    archive, manifest = do_backup(machine)
    roots = new_roots(machine)
    selections, core_map = selections_for(manifest, roots)
    ok, msg = pp.RestoreEngine(archive, selections, core_map).run()
    assert ok, msg
    restored = (roots["chrome"] / "Default/Bookmarks").stat().st_mtime
    assert abs(restored - old) < 24 * 3600  # zip stores local time, 2s granularity


# ------------------------------------------------- verification rejections


def assert_refused(machine, archive, needle: str):
    roots = new_roots(machine)
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read(pp.MANIFEST_NAME))
    selections, core_map = selections_for(manifest, roots)
    ok, msg = pp.RestoreEngine(archive, selections, core_map).run()
    assert not ok
    assert needle in msg, msg
    # nothing written, nothing renamed
    assert not (machine / "New").exists() or not any((machine / "New").rglob("*"))
    return msg


def test_tampered_member_rejected(machine):
    archive, _ = do_backup(machine)
    bad = machine / "bad.zip"
    rewrite_zip(archive, bad, lambda n, d:
                (n, b"TAMPERED") if n == "chrome/root/Default/Bookmarks" else (n, d))
    assert_refused(machine, bad, "mismatch")


def test_tampered_manifest_digest_rejected(machine):
    archive, _ = do_backup(machine)
    bad = machine / "bad.zip"

    def edit(m):
        m["files"][0]["sha256"] = "0" * 64
    edit_manifest(archive, bad, edit)
    assert_refused(machine, bad, "mismatch")


def test_tampered_chain_head_rejected(machine):
    archive, _ = do_backup(machine)
    bad = machine / "bad.zip"
    edit_manifest(archive, bad, lambda m: m.update(chain_head="f" * 64))
    assert_refused(machine, bad, "chain head mismatch")


def test_reordered_manifest_rejected(machine):
    """Chain binds pathnames: swapping two entries must fail even though
    every individual digest is still correct."""
    archive, _ = do_backup(machine)
    bad = machine / "bad.zip"

    def edit(m):
        m["files"][0], m["files"][1] = m["files"][1], m["files"][0]
    edit_manifest(archive, bad, edit)
    assert_refused(machine, bad, "chain")


def test_missing_member_rejected(machine):
    archive, _ = do_backup(machine)
    bad = machine / "bad.zip"
    rewrite_zip(archive, bad, lambda n, d:
                None if n == "chrome/root/Default/Bookmarks" else (n, d))
    assert_refused(machine, bad, "mismatch")


def test_undeclared_member_rejected(machine):
    archive, _ = do_backup(machine)
    bad = machine / "bad.zip"
    rewrite_zip(archive, bad, lambda n, d:
                [(n, d), ("chrome/root/EXTRA.bin", b"x")]
                if n == pp.MANIFEST_NAME else (n, d))
    assert_refused(machine, bad, "mismatch")


def test_duplicate_member_rejected(machine):
    archive, _ = do_backup(machine)
    bad = machine / "bad.zip"
    rewrite_zip(archive, bad, lambda n, d:
                [(n, d), (n, d)]
                if n == "chrome/root/Default/Bookmarks" else (n, d))
    assert_refused(machine, bad, "duplicate member")


def test_traversal_member_rejected(machine):
    archive, _ = do_backup(machine)
    bad = machine / "bad.zip"

    evil_arc = "chrome/root/../../escape.txt"

    def mutate(n, d):
        if n == pp.MANIFEST_NAME:
            m = json.loads(d)
            digest = pp.hashlib.sha256(b"evil").hexdigest()
            m["files"].append({"arc": evil_arc, "sha256": digest,
                               "bytes": 4, "chain": "0" * 64})
            return [(n, json.dumps(m)), (evil_arc, b"evil")]
        return n, d
    rewrite_zip(archive, bad, mutate)
    assert_refused(machine, bad, "illegal path component")


def test_absolute_and_backslash_names_rejected():
    for name in ("/etc/passwd", "C:/x/y", "chrome\\root\\x", "chrome/root/..",
                 "chrome/root//x", "loose.txt"):
        with pytest.raises(pp.ArchiveError):
            pp.validate_member_name(name)
    pp.validate_member_name("chrome/root/Default/Bookmarks")  # sanity


def test_containment_sibling_prefix():
    """C:/Profiles/DaddyMalicious must not pass as inside C:/Profiles/Dad."""
    with pytest.raises(pp.RestoreAborted):
        pp.RestoreEngine._contained(Path("/tmp/Dad"), "../DaddyMalicious/x")


def test_unknown_browser_id_rejected(machine):
    archive, _ = do_backup(machine)
    bad = machine / "bad.zip"
    edit_manifest(archive, bad,
                  lambda m: m["browsers"][0].update(id="netscape9"))
    with zipfile.ZipFile(bad) as zf:
        with pytest.raises(pp.ArchiveError, match="unknown browser id"):
            pp.load_and_validate_manifest(zf)


def test_future_format_rejected(machine):
    archive, _ = do_backup(machine)
    bad = machine / "bad.zip"
    edit_manifest(archive, bad, lambda m: m.update(format=99))
    with zipfile.ZipFile(bad) as zf:
        with pytest.raises(pp.ArchiveError, match="unsupported archive format"):
            pp.load_and_validate_manifest(zf)


def test_malformed_manifest_rejected(machine):
    archive, _ = do_backup(machine)
    bad = machine / "bad.zip"
    rewrite_zip(archive, bad, lambda n, d:
                (n, b"{not json") if n == pp.MANIFEST_NAME else (n, d))
    with zipfile.ZipFile(bad) as zf:
        with pytest.raises(pp.ArchiveError, match="not valid JSON"):
            pp.load_and_validate_manifest(zf)


# ------------------------------------------------- safety-rename failures


def test_preservation_failure_aborts(machine, monkeypatch):
    archive, manifest = do_backup(machine)
    roots = new_roots(machine)
    (roots["chrome"] / "Default").mkdir(parents=True)
    (roots["chrome"] / "Default/marker.txt").write_text("OLD", encoding="utf-8")

    real_rename = Path.rename

    def failing_rename(self, target):
        if self.name == "Default":
            raise OSError("simulated: access denied")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    selections, core_map = selections_for(manifest, roots)
    ok, msg = pp.RestoreEngine(archive, selections, core_map).run()
    assert not ok
    assert "could not preserve existing data" in msg
    # original untouched, nothing restored next to it
    assert (roots["chrome"] / "Default/marker.txt").read_text(
        encoding="utf-8") == "OLD"
    assert not (roots["chrome"] / "Default/Bookmarks").exists()


# ------------------------------------------------- backup-side protections


def test_partial_backup_preserves_previous_archive(machine):
    archive, _ = do_backup(machine)
    good = archive.read_bytes()

    calls = {"n": 0}

    def cancel_after_a_few():
        calls["n"] += 1
        return calls["n"] > 3

    states = pp.scan_all()
    ok, msg = pp.BackupEngine([(s, list(s.profiles)) for s in states],
                              archive, cancel_check=cancel_after_a_few).run()
    assert not ok and "cancelled" in msg.lower()
    assert archive.read_bytes() == good, "previous good archive was destroyed"
    assert not archive.with_name(archive.name + ".partial").exists()


def test_backup_progress_includes_core_files(machine):
    states = pp.scan_all()
    archive = machine / "b.zip"
    seen = []
    ok, _ = pp.BackupEngine([(s, list(s.profiles)) for s in states], archive,
                            on_progress=lambda ph, d, t: seen.append((d, t))).run()
    assert ok
    done, total = seen[-1]
    assert done == total, "progress did not account for all bytes (core files)"


# ------------------------------------------------- v1.1 transactional restore


def leftovers(root: Path) -> list[str]:
    if not root.exists():
        return []
    return [p.name for p in root.rglob("*")
            if ".staging_" in p.name]


def test_no_staging_leftovers_after_success(machine):
    archive, manifest = do_backup(machine)
    roots = new_roots(machine)
    selections, core_map = selections_for(manifest, roots)
    ok, msg = pp.RestoreEngine(archive, selections, core_map).run()
    assert ok, msg
    for root in roots.values():
        assert not leftovers(root.parent), leftovers(root.parent)


def test_cancel_during_staging_leaves_targets_untouched(machine):
    archive, manifest = do_backup(machine)
    roots = new_roots(machine)
    (roots["chrome"] / "Default").mkdir(parents=True)
    (roots["chrome"] / "Default/marker.txt").write_text("OLD", encoding="utf-8")

    calls = {"n": 0}

    def cancel_after_a_few():
        calls["n"] += 1
        return calls["n"] > 2

    selections, core_map = selections_for(manifest, roots)
    ok, msg = pp.RestoreEngine(archive, selections, core_map,
                               cancel_check=cancel_after_a_few).run()
    assert not ok and "nothing at the restore targets was modified" in msg
    # pre-existing target intact, nothing restored, no asides, no staging
    assert (roots["chrome"] / "Default/marker.txt").read_text(
        encoding="utf-8") == "OLD"
    assert not (roots["chrome"] / "Default/Bookmarks").exists()
    for root in roots.values():
        if root.exists():
            assert not any(".pre_restore_" in p.name for p in root.iterdir())
            assert not leftovers(root.parent)


def test_activation_failure_rolls_back(machine, monkeypatch):
    """Default activates first; renaming existing 'Profile 1' aside then
    fails. Everything — including the already-activated Default — must be
    rolled back to its original state."""
    archive, manifest = do_backup(machine)
    roots = new_roots(machine)
    for prof in ("Default", "Profile 1"):
        (roots["chrome"] / prof).mkdir(parents=True)
        (roots["chrome"] / prof / "marker.txt").write_text("OLD", encoding="utf-8")

    real_rename = Path.rename

    def failing_rename(self, target):
        if self.name == "Profile 1":
            raise OSError("simulated: access denied")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    selections, core_map = selections_for(manifest, roots)
    ok, msg = pp.RestoreEngine(archive, selections, core_map).run()
    assert not ok
    assert "rolled back" in msg and "ROLLBACK INCOMPLETE" not in msg

    for prof in ("Default", "Profile 1"):
        assert (roots["chrome"] / prof / "marker.txt").read_text(
            encoding="utf-8") == "OLD", f"{prof} not rolled back"
        assert not (roots["chrome"] / prof / "Bookmarks").exists()
    assert not any(".pre_restore_" in p.name
                   for p in roots["chrome"].iterdir())
    assert not leftovers(roots["chrome"])


def test_verify_engine_ok_and_rejects_tampered(machine):
    archive, _ = do_backup(machine)
    ok, msg = pp.VerifyEngine(archive).run()
    assert ok and "Integrity verified" in msg

    bad = machine / "bad.zip"
    rewrite_zip(archive, bad, lambda n, d:
                (n, b"TAMPERED") if n == "chrome/root/Default/Bookmarks"
                else (n, d))
    ok, msg = pp.VerifyEngine(bad).run()
    assert not ok and "FAILED" in msg


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
