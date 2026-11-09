"""One decision contract shared by chat, autonomy and the MCP surface."""

from typing import Any

from .agent_instructions import runtime_instructions


def decision_policy() -> str:
    return runtime_instructions()

PROJECT_FIELDS = ("phase", "summary", "layout", "remaining", "nextAction", "blocker", "prerequisites", "window",
                  "progress", "reason", "farmRegion", "completionCriteria", "openQuestions")


def project_context(project: Any) -> dict[str, Any]:
    """Bound the current plan without discarding its durable field layout."""
    if not isinstance(project, dict):
        return {}
    previous = project.get("contextTruncated")
    truncated = dict(previous) if isinstance(previous, dict) else {}

    def preview(value: Any, path: str, depth: int = 0) -> Any:
        if isinstance(value, str):
            result = value[:480 if path in {"summary", "layout"} else 240]
        elif isinstance(value, (list, dict)):
            limit = 0 if depth >= 4 else (8 if path == "remaining" or isinstance(value, dict) else 4)
            if isinstance(value, list):
                result = [preview(item, f"{path}[{index}]", depth + 1) for index, item in enumerate(value[:limit])]
            else:
                result = {key: preview(item, f"{path}.{key}", depth + 1) for key, item in list(value.items())[:limit]}
        else:
            return value
        if len(result) < len(value):
            truncated[path] = {"shown": len(result), "total": len(value)}
        return result

    result = {key: preview(project[key], key) for key in PROJECT_FIELDS if key in project}
    if truncated:
        # Projection metadata only; never written back into project notes.
        result["contextTruncated"] = truncated
        result["detailsHint"] = "Query manage_arrangements manage_goal(action=list) for full project notes."
    return result
