"""Bind learning discussions to actual player messages, not generated claims."""
from __future__ import annotations

from typing import Any

from .native_capabilities import CapabilityError


def learning_provenance(*, save_id: str, request_id: str | None, lease: dict,
                        quote: str | None, notice: dict | None = None,
                        previous: dict | None = None) -> dict[str, Any]:
    """The model interprets permission; the runtime preserves its player source.

    No keyword classifier turns conversation into a rigid approval language.
    A previous discussion can cover further iterations of the same capability.
    """
    if quote and quote.strip():
        text = lease.get("playerText")
        if (request_id and lease.get("requestId") == request_id and lease.get("saveId") == save_id
                and isinstance(text, str) and quote in text):
            return {"source": "player_message", "saveId": save_id, "requestId": request_id,
                    "quote": quote, "playerText": text, "noticeId": lease.get("noticeId")}
        if (notice and notice.get("answeredAt") and notice.get("answerRequestId")
                and isinstance(notice.get("answer"), str) and quote in notice["answer"]):
            return {"source": "player_decision", "saveId": save_id, "requestId": notice["answerRequestId"],
                    "quote": quote, "playerText": notice["answer"], "noticeId": notice["id"],
                    "discussion": notice.get("message", "")}
        raise CapabilityError("LEARNING_DISCUSSION_NOT_FOUND: quote the player's actual permission to learn this capability, or answer the linked player decision first.")
    if (previous and previous.get("saveId") == save_id
            and previous.get("source") in {"player_message", "player_decision"}
            and previous.get("requestId") and previous.get("quote")):
        return dict(previous)
    raise CapabilityError("LEARNING_DISCUSSION_REQUIRED: discuss the missing capability and its trial with the player, then quote their permission. Existing permission covers improvements within the same scope.")
