"""Opt-in local experiment images; models select catalog IDs, never file paths."""
import hashlib
import json
from pathlib import Path

from mcp.server.fastmcp import Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent


def read_reference_image(image_id: str | None = None, manifest_path: Path | None = None) -> CallToolResult:
    entries = []
    root = None
    if manifest_path is not None and manifest_path.exists():
        path = Path(manifest_path).resolve()
        root = path.parent
        try:
            entries = json.loads(path.read_text(encoding="utf-8-sig"))["images"]
            if not isinstance(entries, list) or len(entries) > 8:
                raise ValueError("expected at most eight selected images")
            ids = [entry["id"] for entry in entries]
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate reference IDs")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ToolError(f"Reference image manifest unavailable: {error}") from None
    catalog = [{key: entry[key] for key in ("id", "title", "author", "source", "sha256")} for entry in entries]
    metadata = {"kind": "reference_image", "images": catalog,
                "purpose": "Optional inspiration, not a building checklist or evidence of the current farm."}
    content = []
    if image_id is not None:
        entry = next((item for item in entries if item["id"] == image_id), None)
        if entry is None:
            raise ToolError("Unknown reference image ID; omit image_id to list available images")
        path = (root / entry["file"]).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ToolError("Reference image must be a bounded file inside its manifest directory")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ToolError("Reference image hash does not match its recorded source")
        fmt = "png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "jpeg" if data.startswith(b"\xff\xd8\xff") else None
        if fmt is None:
            raise ToolError("Reference image must be PNG or JPEG")
        content.append(Image(data=data, format=fmt).to_image_content())
        metadata["images"] = [item for item in catalog if item["id"] == image_id]
    content.append(TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)))
    return CallToolResult(content=content, structuredContent=metadata)
