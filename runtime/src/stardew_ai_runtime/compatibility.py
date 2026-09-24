"""Offline package compatibility gate; no provider or game mutation."""
import hashlib
import json
from pathlib import Path


class CompatibilityError(RuntimeError):
    pass

def assert_native_compatible(run_dir):
    if not run_dir:
        raise CompatibilityError("COMPATIBILITY_UNKNOWN：尚未绑定游戏实例，未执行动作。")
    root = Path(__file__).resolve().parents[3]
    releases_dir = (root / "artifacts/releases").resolve()
    installed = root / "config/installed-candidate.json"
    manifest = None
    is_local_dev = False

    if installed.is_file():
        try:
            installed_data = json.loads(installed.read_text(encoding="utf-8-sig"))
            selected = Path(installed_data["manifest"]).resolve()
            selected.relative_to(releases_dir)
            manifest = selected
            if installed_data.get("version") == "local-dev" or installed_data.get("manifestType") == "local-dev":
                is_local_dev = True
        except (ValueError, KeyError, TypeError) as error:
            raise CompatibilityError("COMPATIBILITY_UNKNOWN：已安装配套清单无效，未执行动作。") from error
    else:
        local_dev_manifest = root / "artifacts/releases/local-dev/manifest.json"
        repair_fallback = root / "artifacts/releases/repair-r43/manifest.json"
        if local_dev_manifest.is_file():
            manifest = local_dev_manifest
            is_local_dev = True
        elif repair_fallback.is_file():
            manifest = repair_fallback

    if manifest is None or not manifest.is_file():
        raise CompatibilityError("COMPATIBILITY_UNKNOWN：缺少配套清单，请先运行设置向导或安装修复包。未执行动作。")

    package = json.loads(manifest.read_text(encoding="utf-8"))
    if package.get("version") == "local-dev" or package.get("manifestType") == "local-dev" or manifest.parent.name == "local-dev":
        is_local_dev = True

    run = Path(run_dir).resolve()
    candidates = [
        run / "StardewAI.Companion.Mod.dll",
        run / "mods/StardewAI.Companion.Mod/StardewAI.Companion.Mod.dll",
        run / "Mods/StardewAI.Companion.Mod/StardewAI.Companion.Mod.dll",
    ]
    dll = next((p for p in candidates if p.is_file()), None)
    if dll is None:
        if is_local_dev:
            raise CompatibilityError(
                "COMPATIBILITY_UNKNOWN：已绑定实例找不到生产DLL，未执行动作；请核对Mod目录并重新运行 tools/build-mod.ps1 与 tools/setup-companion.ps1。"
            )
        raise CompatibilityError(
            "COMPATIBILITY_UNKNOWN：已绑定实例找不到生产DLL，未执行动作；请核对Mod目录，并使用随修复包提供的更新脚本或重新运行 tools/setup-companion.ps1。"
        )

    actual = hashlib.sha256(dll.read_bytes()).hexdigest().upper()
    expected = package.get("modSha256", "")
    if actual != expected:
        if is_local_dev:
            raise CompatibilityError(
                f"MOD_RUNTIME_MISMATCH：当前Mod {actual[:12]} 与配套运行时不匹配（需要 {expected[:12]}）。请退出游戏和伙伴服务，运行 tools/build-mod.ps1 与 tools/setup-companion.ps1 重新安装。未执行游戏动作。"
            )
        raise CompatibilityError(
            f"MOD_RUNTIME_MISMATCH：当前Mod {actual[:12]} 与配套运行时不匹配（需要 {expected[:12]}）。请退出游戏和伙伴服务，使用随修复包提供的更新脚本或重新运行 tools/setup-companion.ps1 安装配对版本；保留现有agy/其他后端配置。未执行游戏动作。"
        )
