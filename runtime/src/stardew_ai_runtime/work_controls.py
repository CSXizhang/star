"""Unambiguous work-control directives, separate from discussion and tool actions.

Only an imperative with a current-work reference or an explicit universal scope
can bypass the conversation model. Named/ambiguous scopes remain model-resolved.
Negated, quoted, conditional and interrogative statements grant no control.
"""
from __future__ import annotations

import re


def explicit_cancel_scope(text: str) -> str | None:
    text = text.strip().rstrip("。！! ")
    if re.search(r"[？?\"“”‘’]|(?:如果|假如|是否|什么意思|怎么|如何|不要取消|别取消|不要撤销)", text):
        return None
    clauses = re.split(r"[，,；;。]", text)
    head = clauses[0].strip()
    match = re.fullmatch(r"(?:请)?(?:取消|撤销)(.*)", head)
    if not match:
        return None
    # Additional clauses must only reinforce stopping; mixed new work belongs
    # to the model, which can bind each action to its particular arrangement.
    if any(not re.fullmatch(r"(?:请)?(?:立即|马上|现在)?(?:停止|停下|不再派工|不要再派工|不要继续|不再执行)", c.strip())
           for c in clauses[1:] if c.strip()):
        return None
    target = match[1].strip()
    if not target or target in {"任务", "工作", "安排", "计划"}:
        return "current"
    if re.fullmatch(r"(?:所有|全部|一切)(?:的)?(?:任务|工作|安排|计划)", target):
        return "all"
    if re.fullmatch(r"(?:刚才|刚刚|当前|现在|这)(?:的|项|个)?[^，,；;。？?]{0,20}(?:任务|工作|安排|计划)", target):
        return "current"
    return None
