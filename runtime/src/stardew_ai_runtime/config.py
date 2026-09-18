from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    game_path: Path | None
    smapi_path: Path | None
    data_dir: Path
    protocol_version: str

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        game_value = os.getenv("STARDEW_GAME_PATH")
        smapi_value = os.getenv("STAR_SMAPI_PATH")
        data_value = os.getenv("STAR_RUNTIME_DATA_DIR", "runtime-data")

        game_path = Path(game_value) if game_value else None
        smapi_path = Path(smapi_value) if smapi_value else None
        if smapi_path is None and game_path is not None:
            smapi_path = game_path / "StardewModdingAPI.exe"

        return cls(
            game_path=game_path,
            smapi_path=smapi_path,
            data_dir=Path(data_value),
            protocol_version=os.getenv("STAR_PROTOCOL_VERSION", "0.1"),
        )

    def health(self) -> dict[str, bool]:
        return {
            "game_path_exists": self.game_path is not None and self.game_path.is_dir(),
            "smapi_path_exists": self.smapi_path is not None and self.smapi_path.is_file(),
            "protocol_supported": self.protocol_version == "0.1",
        }
