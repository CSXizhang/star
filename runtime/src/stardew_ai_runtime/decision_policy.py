"""One decision contract shared by chat, autonomy and the MCP surface."""

from typing import Any

from .agent_instructions import runtime_instructions


def decision_policy() -> str:
    return runtime_instructions()

PROJECT_FIELDS = ("phase", "summary", "nextAction", "blocker", "prerequisites", "window",
                  "progress", "reason", "farmRegion", "completionCriteria", "openQuestions")


def project_context(project: Any) -> dict[str, Any]:
    """Bound the current plan without discarding its durable field layout."""
    if not isinstance(project, dict):
        return {}
    result = {key: project[key] for key in PROJECT_FIELDS if key in project}
    for key, value in result.items():
        if isinstance(value, str):
            result[key] = value[:480 if key == "summary" else 240]
        elif isinstance(value, list):
            result[key] = value[:4]
    return result
