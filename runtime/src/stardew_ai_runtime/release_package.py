"""Integrity/pairing checks for the relocatable player distribution."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath

MOD_DLL = "StardewAI.Companion.Mod.dll"
REQUIRED = {MOD_DLL, "manifest.json", "runtime/python/python.exe",
            "runtime/src/stardew_ai_runtime/chat_bridge.py", "tools/start-companion.ps1"}


def managed_path(root: Path, relative: str) -> Path:
    normalized = relative.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if not parts or any(p in {".", ".."} for p in parts) or ":" in normalized:
        raise ValueError(f"Unsafe package path: {relative}")
    target = (root / normalized).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise ValueError(f"Package path escapes root: {relative}")
    if parts[0].lower() in {"data", "config", ".kimi-code", ".env", ".env.local"}:
        raise ValueError(f"Release cannot own player settings: {relative}")
    return target


def verify_release(root: Path, *, full: bool = True) -> dict:
    """Full audit for publishing/explicit verification; normal use checks the pair only."""
    root = root.resolve()
    package = json.loads((root / "release-manifest.json").read_text(encoding="utf-8-sig"))
    if package.get("manifestType") != "windows-release" or package.get("schemaVersion") != 1:
        raise ValueError("Unsupported release manifest")
    if package.get("python") != "runtime/python/python.exe" or package.get("platform") != "windows-x64":
        raise ValueError("Unsupported release runtime layout")
    seen = set()
    hashes = {}
    for entry in package.get("files", []):
        relative = entry["path"]
        canonical = relative.replace("\\", "/").casefold()
        if canonical in seen:
            raise ValueError(f"Duplicate release file: {relative}")
        seen.add(canonical)
        if not full and relative not in REQUIRED:
            continue
        target = managed_path(root, relative)
        if not target.is_file():
            raise ValueError(f"Release file missing: {relative}")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != entry["sha256"].lower():
            raise ValueError(f"Release file changed: {relative}; reinstall the matching package")
        hashes[relative] = actual
    if not REQUIRED.issubset(hashes):
        raise ValueError("Release manifest omits required production files")
    native = json.loads((root / "manifest.json").read_text(encoding="utf-8-sig"))
    if (hashes[MOD_DLL] != package.get("modSha256", "").lower()
            or native.get("Version") != package.get("version")
            or native.get("EntryDll") != MOD_DLL):
        raise ValueError("Mod/runtime release pairing differs")
    return package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path, required=True)
    args = parser.parse_args()
    package = verify_release(args.verify)
    print(json.dumps({"valid": True, "version": package["version"], "files": len(package["files"])}))


if __name__ == "__main__":
    main()
