"""Run the real Windows updater against disposable installations; no game launch."""
import ctypes
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from test_release_package import package_at

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows updater")


def package(root, version):
    doc = package_at(root)
    long_relative = "runtime/python/Lib/site-packages/jsonschema_specifications/schemas/draft202012/vocabularies/format-annotation"
    long_file = root / long_relative
    long_file.parent.mkdir(parents=True, exist_ok=True)
    long_file.write_text(version)
    doc["files"].append({"path": long_relative, "sha256": ""})
    native = root / "manifest.json"
    native.write_text(json.dumps({"Version": version, "EntryDll": "StardewAI.Companion.Mod.dll",
                                 "UniqueID": "CSXizhang.StardewAICompanion", "MinimumApiVersion": "4.2.1"}))
    (root / "StardewAI.Companion.Mod.dll").write_bytes(version.encode())
    doc["version"] = version
    for entry in doc["files"]:
        entry["sha256"] = hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest()
    doc["modSha256"] = hashlib.sha256((root / "StardewAI.Companion.Mod.dll").read_bytes()).hexdigest()
    (root / "release-manifest.json").write_text(json.dumps(doc))
    return doc


def setup(tmp_path):
    game = tmp_path / "游戏 空格"
    target = game / "Mods/Companion"
    package(target, "0.3.14")
    (game / "StardewModdingAPI.exe").write_bytes(b"not executed")
    (target / "config.json").write_text('{"Mode":"player"}')
    (target / "data").mkdir()
    (target / "data/memory.json").write_text('{"keep":true}')
    source = tmp_path / "new"
    package(source, "0.3.15")
    work = target / "data/updates/test"
    work.mkdir(parents=True)
    archive = work / "package.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for f in source.rglob("*"):
            if f.is_file():
                z.write(f, "StardewAI.Companion.Mod/" + f.relative_to(source).as_posix())
    for name in ("apply-update.ps1", "release-package.ps1"):
        shutil.copyfile(ROOT / "tools" / name, work / name)
    request = {"target": str(target), "game": str(game), "processId": 2000000000,
               "processStarted": "", "archive": str(archive),
               "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
               "version": "0.3.15", "installedVersion": "0.3.14", "smapi": "4.2.1"}
    (work / "request.json").write_text(json.dumps(request), encoding="utf-8")
    return target, work


def run(work, prepare=False):
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                           str(work / "apply-update.ps1"), "-Request", str(work / "request.json"),
                           "-NoRestart", *(["-Prepare"] if prepare else [])],
                          capture_output=True, timeout=60)


def test_update_preserves_mode_and_memory(tmp_path):
    target, work = setup(tmp_path)
    prepared = run(work, True)
    assert prepared.returncode == 0, prepared.stderr
    assert (target / "StardewAI.Companion.Mod.dll").read_bytes() == b"0.3.14"
    applied = run(work)
    assert applied.returncode == 0, applied.stderr
    assert (target / "StardewAI.Companion.Mod.dll").read_bytes() == b"0.3.15"
    assert json.loads((target / "config.json").read_text())["Mode"] == "player"
    assert json.loads((target / "data/memory.json").read_text()) == {"keep": True}


@pytest.mark.parametrize("corrupt", ["hash", "traversal", "settings", "smapi"])
def test_invalid_update_never_changes_install(tmp_path, corrupt):
    target, work = setup(tmp_path)
    request = json.loads((work / "request.json").read_text())
    if corrupt in {"traversal", "settings"}:
        with zipfile.ZipFile(work / "package.zip", "a") as z:
            z.writestr("StardewAI.Companion.Mod/" + ("../escape" if corrupt == "traversal" else "config.json"), "bad")
        request["sha256"] = hashlib.sha256((work / "package.zip").read_bytes()).hexdigest()
    elif corrupt == "hash":
        request["sha256"] = "0" * 64
    else:
        request["smapi"] = "4.0.0"
    (work / "request.json").write_text(json.dumps(request))
    assert run(work, True).returncode != 0
    assert (target / "StardewAI.Companion.Mod.dll").read_bytes() == b"0.3.14"
    assert json.loads((target / "config.json").read_text())["Mode"] == "player"


def test_locked_file_restores_prior_files(tmp_path):
    target, work = setup(tmp_path)
    assert run(work, True).returncode == 0
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    # Sharing permits backup/read, but denies replacement of the DLL.
    handle = kernel.CreateFileW(str(target / "StardewAI.Companion.Mod.dll"), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        result = run(work)
        assert result.returncode != 0
    finally:
        kernel.CloseHandle(handle)
    assert json.loads((target / "manifest.json").read_text())["Version"] == "0.3.14"
    assert (target / "StardewAI.Companion.Mod.dll").read_bytes() == b"0.3.14"
    assert json.loads((target / "data/update-transaction.json").read_text(encoding="utf-8"))["state"] == "restored"


def test_retry_recovers_interrupted_copy_before_installing(tmp_path):
    target, work = setup(tmp_path)
    assert run(work, True).returncode == 0
    backup = work / "interrupted-backup"
    backup.mkdir()
    journal = []
    for relative in ("manifest.json", "StardewAI.Companion.Mod.dll"):
        shutil.copyfile(target / relative, backup / relative)
        journal.append({"path": relative, "existed": True,
                        "sha256": hashlib.sha256((backup / relative).read_bytes()).hexdigest()})
        (target / relative).write_bytes(b"interrupted")
    (target / "data/update-transaction.json").write_text(json.dumps({
        "state": "installing", "backup": str(backup), "files": journal}))
    result = run(work)
    assert result.returncode == 0, result.stderr
    assert (target / "StardewAI.Companion.Mod.dll").read_bytes() == b"0.3.15"
    assert json.loads((target / "config.json").read_text())["Mode"] == "player"
