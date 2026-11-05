"""File logging and run-directory discovery for the chat bridge."""

from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from stardew_ai_runtime.chat_backend_config import project_root
from stardew_ai_runtime.scheduler import (
    get_candidate_discovery_paths,
)

logger = logging.getLogger("stardew_ai_runtime.chat_bridge")

CHAT_BRIDGE_LOG_FILENAME = "chat-bridge.log"
CHAT_BRIDGE_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
CHAT_BRIDGE_LOG_BACKUP_COUNT = 3
CHAT_BRIDGE_LOG_ENCODING = "utf-8"
CHAT_BRIDGE_LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
CHAT_BRIDGE_LOG_DATEFMT = "%H:%M:%S"


def resolve_chat_bridge_run_dir(run_dir: str | Path | None = None) -> Path | None:
    """Resolves the active run directory, consulting explicit path, environment, and auto-discovery."""
    if run_dir:
        return Path(run_dir).resolve()
    env_dir = os.getenv("STARDEW_RUN_DIR")
    if env_dir and env_dir.strip():
        return Path(env_dir.strip()).resolve()
    try:
        candidate_meta = project_root() / "config" / "installed-candidate.json"
        if candidate_meta.is_file():
            meta = json.loads(candidate_meta.read_text(encoding="utf-8"))
            mod_dir = meta.get("modDirectory")
            if mod_dir and Path(mod_dir).is_dir():
                return Path(mod_dir).resolve()
    except Exception:
        pass
    try:
        for cand in get_candidate_discovery_paths(None):
            if cand.is_file():
                parent = cand.parent
                mod_dir = parent.parent if parent.name.lower() == "data" else parent
                return mod_dir.resolve()
            parent = cand.parent
            cand_mod = parent.parent if parent.name.lower() == "data" else parent
            if cand_mod.is_dir():
                return cand_mod.resolve()
    except Exception:
        pass
    return None


def get_chat_bridge_fallback_log_dir() -> Path:
    """Returns the fallback directory for logs when run-dir cannot be resolved."""
    env_override = os.getenv("STARDEW_FALLBACK_LOG_DIR")
    if env_override and env_override.strip():
        return Path(env_override.strip())
    return Path.home() / ".gemini" / "antigravity-cli" / "logs"


def get_chat_bridge_log_path(run_dir: str | Path | None = None) -> Path:
    """Determines the destination path for chat-bridge.log."""
    resolved = resolve_chat_bridge_run_dir(run_dir)
    if resolved is not None:
        return resolved / "logs" / CHAT_BRIDGE_LOG_FILENAME
    return get_chat_bridge_fallback_log_dir() / CHAT_BRIDGE_LOG_FILENAME


def has_chat_bridge_file_logging() -> bool:
    """Checks whether a ChatBridge RotatingFileHandler is currently attached to the root logger."""
    root_logger = logging.getLogger()
    for h in root_logger.handlers:
        if getattr(h, "_is_chat_bridge_handler", False):
            return True
    return False


def configure_chat_bridge_file_logging(
    run_dir: str | Path | None = None,
) -> tuple[RotatingFileHandler, Path]:
    """Attaches a 5MB x 3 UTF-8 RotatingFileHandler to the root logger."""
    log_path = get_chat_bridge_log_path(run_dir)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as ex:
        fallback_path = get_chat_bridge_fallback_log_dir() / CHAT_BRIDGE_LOG_FILENAME
        try:
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            log_path = fallback_path
        except Exception:
            logger.warning("Failed to create log directories (%s); falling back to current working directory", ex)
            log_path = Path.cwd() / "logs" / CHAT_BRIDGE_LOG_FILENAME
            log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    for h in root_logger.handlers:
        if getattr(h, "_is_chat_bridge_handler", False) and isinstance(h, RotatingFileHandler):
            try:
                if Path(h.baseFilename).resolve() == log_path.resolve():
                    return h, log_path
            except Exception:
                pass

    handler = RotatingFileHandler(
        str(log_path),
        maxBytes=CHAT_BRIDGE_LOG_MAX_BYTES,
        backupCount=CHAT_BRIDGE_LOG_BACKUP_COUNT,
        encoding=CHAT_BRIDGE_LOG_ENCODING,
    )
    handler.setLevel(logging.INFO)
    handler._is_chat_bridge_handler = True  # type: ignore[attr-defined]
    formatter = logging.Formatter(
        CHAT_BRIDGE_LOG_FORMAT,
        datefmt=CHAT_BRIDGE_LOG_DATEFMT,
    )
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    if root_logger.level > logging.INFO or root_logger.level == logging.NOTSET:
        root_logger.setLevel(logging.INFO)
    return handler, log_path


def switch_chat_bridge_file_logging(
    new_run_dir: str | Path | None,
) -> tuple[RotatingFileHandler, Path]:
    """Switches the active chat_bridge file handler to a newly discovered run directory."""
    log_path = get_chat_bridge_log_path(new_run_dir)
    root_logger = logging.getLogger()
    for h in list(root_logger.handlers):
        if getattr(h, "_is_chat_bridge_handler", False) and isinstance(h, RotatingFileHandler):
            try:
                if Path(h.baseFilename).resolve() == log_path.resolve():
                    return h, log_path
                h.close()
                root_logger.removeHandler(h)
            except Exception:
                pass
    return configure_chat_bridge_file_logging(new_run_dir)


def remove_chat_bridge_file_logging() -> None:
    """Closes and removes all chat_bridge file handlers from the root logger."""
    root_logger = logging.getLogger()
    for h in list(root_logger.handlers):
        if getattr(h, "_is_chat_bridge_handler", False):
            try:
                h.close()
            except Exception:
                pass
            root_logger.removeHandler(h)
