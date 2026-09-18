from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_BACKEND = "kimi"
DEFAULT_MODEL = "kimi-code/k3"


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def config_path() -> Path:
    override = os.getenv("STARDEW_CHAT_BACKEND_CONFIG")
    if override:
        return Path(override)
    return project_root() / "config" / "chat-backend.json"


def load_chat_backend_config(path: Path | None = None) -> dict[str, str]:
    target = path or config_path()
    try:
        raw: Any = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("backend") in {"agy", "kimi"}:
            config = {"backend": str(raw["backend"]), "model": str(raw.get("model") or DEFAULT_MODEL)}
            if raw.get("agent"):
                config["agent"] = str(raw["agent"])
            if raw.get("agentFile"):
                config["agentFile"] = str(raw["agentFile"])
            return config
    except (OSError, ValueError, TypeError):
        pass
    return {"backend": DEFAULT_BACKEND, "model": DEFAULT_MODEL}
