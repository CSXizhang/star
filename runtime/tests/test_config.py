from pathlib import Path

from stardew_ai_runtime.config import RuntimeSettings


def test_settings_derive_smapi_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STARDEW_GAME_PATH", str(tmp_path))
    monkeypatch.delenv("STAR_SMAPI_PATH", raising=False)
    monkeypatch.setenv("STAR_PROTOCOL_VERSION", "0.1")

    settings = RuntimeSettings.from_environment()

    assert settings.game_path == tmp_path
    assert settings.smapi_path == tmp_path / "StardewModdingAPI.exe"
    assert settings.protocol_version == "0.1"


def test_health_rejects_unknown_protocol(tmp_path: Path) -> None:
    smapi = tmp_path / "StardewModdingAPI.exe"
    smapi.touch()
    settings = RuntimeSettings(tmp_path, smapi, tmp_path / "data", "9.9")

    assert settings.health() == {
        "game_path_exists": True,
        "smapi_path_exists": True,
        "protocol_supported": False,
    }
