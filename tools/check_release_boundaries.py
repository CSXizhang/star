from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOT = REPOSITORY_ROOT / "src" / "StardewAI.Companion.Mod"
FORBIDDEN_MARKERS = (
    "TestDriver",
    "golden-saves",
    "direct world setup",
    "test scenario prepare",
)


def main() -> int:
    violations: list[str] = []
    for path in PRODUCTION_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".cs", ".csproj", ".json"}:
            continue
        content = path.read_text(encoding="utf-8")
        for marker in FORBIDDEN_MARKERS:
            if marker.casefold() in content.casefold():
                violations.append(f"{path.relative_to(REPOSITORY_ROOT)}: {marker}")

    if violations:
        print("Production boundary violations detected:")
        for violation in violations:
            print(f"- {violation}")
        return 1

    print("Production/TestDriver boundary scan passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
