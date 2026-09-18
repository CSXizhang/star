from __future__ import annotations

import argparse
import json
import sys

from stardew_ai_runtime.config import RuntimeSettings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stardew-ai-runtime")
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the local runtime baseline health as JSON",
    )
    args = parser.parse_args(argv)

    if not args.check:
        parser.print_help()
        return 0

    settings = RuntimeSettings.from_environment()
    health = settings.health()
    result = {
        "protocolVersion": settings.protocol_version,
        "gamePath": str(settings.game_path) if settings.game_path else None,
        "smapiPath": str(settings.smapi_path) if settings.smapi_path else None,
        "dataDir": str(settings.data_dir),
        "health": health,
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if all(health.values()) else 1
