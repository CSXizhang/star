import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def package_at(root):
    files = {
        "manifest.json": json.dumps({"Version": "0.2.0", "EntryDll": "StardewAI.Companion.Mod.dll"}).encode(),
        "StardewAI.Companion.Mod.dll": b"fixture-dll",
        "runtime/python/python.exe": b"fixture-python-not-executed",
        "runtime/src/stardew_ai_runtime/chat_bridge.py": b"# fixture-runtime",
    }
    for name in ("setup-companion.ps1", "install-release.ps1", "release-package.ps1", "register-mcp.ps1", "start-companion.ps1", "detect-game.ps1"):
        files["tools/" + name] = (ROOT / "tools" / name).read_bytes()
    skill = "agent-skills/stardew-companion/SKILL.md"
    files[skill] = (ROOT / skill).read_bytes()
    from stardew_ai_runtime.agent_instructions import INSTRUCTION_FILES
    for path in INSTRUCTION_FILES:
        files[path.as_posix()] = (ROOT / path).read_bytes()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    doc = {
        "manifestType": "windows-release", "schemaVersion": 1, "version": "0.2.0",
        "platform": "windows-x64", "python": "runtime/python/python.exe",
        "modSha256": hashlib.sha256(files["StardewAI.Companion.Mod.dll"]).hexdigest(),
        "files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest()} for name, content in files.items()],
    }
    (root / "release-manifest.json").write_text(json.dumps(doc), encoding="utf-8")
    return doc
