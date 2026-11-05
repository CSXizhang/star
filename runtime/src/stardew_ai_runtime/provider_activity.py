"""Shared inactivity boundary; native work retains its own action watchdog."""


def inactivity_deadline(task, deadline: float, interval: float) -> float:
    progress = getattr(task, "native_progress_at", None)
    return max(deadline, progress + interval) if type(progress) in {int, float} else deadline
