"""Offline package compatibility gate; no provider or game mutation."""
from pathlib import Path
import hashlib
import json

class CompatibilityError(RuntimeError):
    pass

def assert_native_compatible(run_dir):
    if not run_dir:
        raise CompatibilityError("COMPATIBILITY_UNKNOWN：尚未绑定游戏实例，未执行动作。")
    root=Path(__file__).resolve().parents[3]
    manifest=root/"artifacts/releases/repair-r43/manifest.json"
    installed=root/"config/installed-candidate.json"
    if installed.is_file():
        try:
            selected=Path(json.loads(installed.read_text(encoding="utf-8-sig"))["manifest"]).resolve()
            selected.relative_to((root/"artifacts/releases").resolve())
            manifest=selected
        except (ValueError, KeyError, TypeError) as error:
            raise CompatibilityError("COMPATIBILITY_UNKNOWN：已安装配套清单无效，未执行动作。") from error
    if not manifest.is_file():
        raise CompatibilityError("COMPATIBILITY_UNKNOWN：缺少配套修复包清单，请使用完整配套更新包。")
    package=json.loads(manifest.read_text(encoding="utf-8"))
    run=Path(run_dir).resolve()
    candidates=[run/"StardewAI.Companion.Mod.dll",run/"mods/StardewAI.Companion.Mod/StardewAI.Companion.Mod.dll",run/"Mods/StardewAI.Companion.Mod/StardewAI.Companion.Mod.dll"]
    dll=next((p for p in candidates if p.is_file()),None)
    if dll is None:
        raise CompatibilityError("COMPATIBILITY_UNKNOWN：已绑定实例找不到生产DLL，未执行动作；请核对Mod目录并使用更新伙伴修复包.cmd。")
    actual=hashlib.sha256(dll.read_bytes()).hexdigest().upper()
    if actual != package["modSha256"]:
        raise CompatibilityError(f"MOD_RUNTIME_MISMATCH：当前Mod {actual[:12]} 与配套运行时不匹配（需要 {package['modSha256'][:12]}）。请退出游戏和伙伴服务，双击 更新伙伴修复包.cmd；保留现有agy/其他后端配置。未执行游戏动作。")
