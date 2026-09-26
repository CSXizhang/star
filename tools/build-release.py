"""Build a relocatable Windows x64 player ZIP; Python/uv are build tools only.

Usage: runtime/.venv/Scripts/python.exe tools/build-release.py --version 0.2.0
       --mod-source artifacts/dist/StardewAI.Companion.Mod
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.13.11"
PYTHON_SHA256 = "1ec066fb61ba5e8c73e29e048cd07c26850f74585e3a116005135b31b8004890"
MOD_FILES = ("manifest.json", "StardewAI.Companion.Mod.dll", "StardewAI.Companion.Mod.deps.json")
TOOL_FILES = ("setup-companion.ps1", "start-companion.ps1", "register-mcp.ps1",
              "candidate-package.ps1", "release-package.ps1", "install-release.ps1", "detect-game.ps1")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args: list[str], **kwargs) -> str:
    result = subprocess.run(args, check=True, text=True, encoding="utf-8", capture_output=True, **kwargs)
    return result.stdout.strip()


def copy_file(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"Missing or linked release input: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def build(args) -> Path:
    if sys.platform != "win32":
        raise ValueError("Build on Windows x64 so the actual bundled interpreter can be tested")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", args.version):
        raise ValueError("Version must be a semantic version, e.g. 0.2.0")
    source = args.mod_source.resolve()
    native = json.loads((source / "manifest.json").read_text(encoding="utf-8-sig"))
    if native.get("Version") != args.version or native.get("EntryDll") != MOD_FILES[1]:
        raise ValueError("Compiled Mod manifest Version/EntryDll does not match requested release")
    if (source / MOD_FILES[1]).read_bytes()[:2] != b"MZ":
        raise ValueError("Production Mod DLL is not a Windows PE assembly")
    output = args.output.resolve()
    stage = output / f"stage-{args.version}"
    package = stage / "StardewAI.Companion.Mod"
    archive = output / f"StardewAI.Companion.Mod-{args.version}-windows-x64.zip"
    if stage.exists() or archive.exists():
        raise ValueError(f"Output already exists; select a fresh --output directory: {stage}")
    package.mkdir(parents=True)
    for name in MOD_FILES:
        copy_file(source / name, package / name)
    # PDBs can embed developer source paths; they are intentionally not shipped.
    for path in sorted((ROOT / "runtime/src/stardew_ai_runtime").rglob("*.py")):
        copy_file(path, package / path.relative_to(ROOT))
    for name in TOOL_FILES:
        copy_file(ROOT / "tools" / name, package / "tools" / name)
    for name in ("设置星露谷伙伴.cmd", "启动伙伴服务.cmd", "LICENSE"):
        copy_file(ROOT / name, package / name)
    guide = (ROOT / "docs/release-guide.md").read_text(encoding="utf-8-sig")
    guide = guide.replace("(companion-guide.md)", "(docs/companion-guide.md)").replace("(mcp.md)", "(docs/mcp.md)").replace("(../CONTRIBUTING.md)", "(CONTRIBUTING.md)")
    (package / "使用说明.md").write_text(guide, encoding="utf-8")
    copy_file(ROOT / "CONTRIBUTING.md", package / "CONTRIBUTING.md")
    for name in ("release-guide.md", "companion-guide.md", "mcp.md"):
        copy_file(ROOT / "docs" / name, package / "docs" / name)

    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    python_zip = cache / f"python-{args.python_version}-embed-amd64.zip"
    if not python_zip.exists():
        url = f"https://www.python.org/ftp/python/{args.python_version}/{python_zip.name}"
        temporary = python_zip.with_suffix(".download")
        with urllib.request.urlopen(url, timeout=90) as response, temporary.open("wb") as target:
            shutil.copyfileobj(response, target)
        temporary.replace(python_zip)
    if digest(python_zip) != args.python_sha256.lower():
        raise ValueError("Official embedded Python SHA256 mismatch; release was not built")
    python_dir = package / "runtime/python"
    python_dir.mkdir(parents=True)
    with zipfile.ZipFile(python_zip) as zipped:
        for member in zipped.infolist():
            resolved = (python_dir / member.filename).resolve()
            if not resolved.is_relative_to(python_dir) or ":" in member.filename:
                raise ValueError("Invalid Python ZIP member")
        zipped.extractall(python_dir)
    version_tag = "".join(args.python_version.split(".")[:2])
    (python_dir / f"python{version_tag}._pth").write_text(
        f"python{version_tag}.zip\n.\nLib/site-packages\n../src\nimport site\n", encoding="utf-8")
    python = python_dir / "python.exe"
    if run([str(python), "-c", "import sys;print(sys.version.split()[0])"]) != args.python_version:
        raise ValueError("Embedded Python version differs from lock")
    requirements = package / "runtime/requirements-release.txt"
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("Build machine needs uv; players do not")
    exported = run([uv, "export", "--project", str(ROOT / "runtime"), "--frozen", "--no-dev",
                    "--no-emit-project", "--no-header", "--format", "requirements-txt"])
    requirements.write_text(exported + "\n", encoding="utf-8")
    site_packages = python_dir / "Lib/site-packages"
    run([uv, "pip", "install", "--python", str(python), "--target", str(site_packages),
         "--require-hashes", "--only-binary", ":all:", "--no-deps", "--no-cache",
         "-r", str(requirements)])
    # uv entry-point wrappers may contain absolute build paths and are not used.
    scripts = site_packages / "Scripts"
    if scripts.exists():
        for path in scripts.rglob("*"):
            if path.is_file():
                path.unlink()
        for path in sorted(scripts.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
        scripts.rmdir()
    for path in site_packages.rglob("direct_url.json"):
        path.unlink()
    licenses = package / "LICENSES"
    copy_file(python_dir / "LICENSE.txt", licenses / "Python-LICENSE.txt")
    dependencies = json.loads(run([str(python), "-c", "import importlib.metadata as m,json;print(json.dumps([{'name':d.metadata['Name'],'version':d.version,'license':d.metadata.get('License-Expression') or d.metadata.get('License','')} for d in m.distributions()]))"]))
    for metadata in site_packages.glob("*.dist-info"):
        for path in metadata.rglob("*"):
            if path.is_file() and any(word in path.name.lower() for word in ("license", "copying", "notice")):
                copy_file(path, licenses / metadata.name / path.relative_to(metadata))
    (licenses / "dependencies.json").write_text(json.dumps(dependencies, ensure_ascii=False, indent=2), encoding="utf-8")
    # Exercise stdlib, native wheels and actual app imports before writing hashes.
    smoke = "import sqlite3,ssl,ctypes,win32api,win32job,mcp,pydantic_core,cryptography;import stardew_ai_runtime.chat_bridge,stardew_ai_runtime.mcp_server;print('portable-runtime-ok')"
    clean_env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "STARDEW_"))}
    run([str(python), "-B", "-c", smoke], env=clean_env, cwd=str(output))
    # All bytecode is disposable, may encode build paths, and must not be shipped.
    for path in package.rglob("*.pyc"):
        path.unlink()
    for path in sorted(package.rglob("__pycache__"), reverse=True):
        path.rmdir()
    manifest = {
        "manifestType": "windows-release", "schemaVersion": 1, "version": args.version,
        "sourceCommit": run(["git", "rev-parse", "HEAD"], cwd=ROOT),
        "sourceDirty": bool(run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT)),
        "platform": "windows-x64", "python": "runtime/python/python.exe",
        "pythonVersion": args.python_version, "pythonArchiveSha256": args.python_sha256.lower(),
        "dependencyLockSha256": digest(ROOT / "runtime/uv.lock"),
        "modSha256": digest(package / MOD_FILES[1]),
        "files": [{"path": path.relative_to(package).as_posix(), "sha256": digest(path)}
                  for path in sorted(package.rglob("*")) if path.is_file()],
    }
    (package / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    run([str(python), "-B", "-m", "stardew_ai_runtime.release_package", "--verify", str(package)], env=clean_env)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(stage))
    archive.with_suffix(".zip.sha256").write_text(f"{digest(archive)}  {archive.name}\n", encoding="ascii")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--mod-source", type=Path, default=ROOT / "artifacts/dist/StardewAI.Companion.Mod")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/releases/windows")
    parser.add_argument("--cache", type=Path, default=ROOT / ".local/release-cache")
    parser.add_argument("--python-version", default=PYTHON_VERSION)
    parser.add_argument("--python-sha256", default=PYTHON_SHA256,
                        help="Required matching SHA256 when overriding the official Python version")
    args = parser.parse_args()
    try:
        print(build(args))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Release build failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
