"""Regress the observed native shipping-menu dismissal between frames."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import continuity_probe


class OvernightRunner:
    def __init__(self, menu_after_error=None):
        self.menu_after_error = menu_after_error
        self.snapshots = 0
        self.actions = []

    async def native(self):
        return {"year": 1, "season": "spring", "day": 2 if self.snapshots > 1 else 1,
                "companionMachineExecuting": None, "companionActiveTaskId": None,
                "companionRestState": "awake"}

    async def request(self, action, **params):
        self.actions.append(action)
        if action == "key":
            raise RuntimeError("Invalid menu key.")
        if action != "snapshot":
            return {}
        self.snapshots += 1
        if self.snapshots == 1:
            return {"playerIsInBed": True, "dialogueResponses": [{"key": "Yes"}]}
        return {"year": 1, "season": "spring", "day": 2, "worldReady": True,
                "nativeNewDay": False, "nativeFade": False,
                "menuType": "ShippingMenu" if self.snapshots == 2 else self.menu_after_error}


async def no_delay(_):
    pass


def test_sleep_reobserves_settled_day_when_shipping_menu_disappears(monkeypatch):
    monkeypatch.setattr(continuity_probe.asyncio, "sleep", no_delay)
    probe = continuity_probe.Probe.__new__(continuity_probe.Probe)
    probe.r = OvernightRunner()
    result = asyncio.run(probe.sleep())
    assert result["before"]["day"] == 1 and result["after"]["day"] == 2
    assert probe.r.snapshots == 4
    assert probe.r.actions[-1] == "leave-player-bed"


def test_sleep_does_not_hide_input_failure_while_a_menu_remains(monkeypatch):
    monkeypatch.setattr(continuity_probe.asyncio, "sleep", no_delay)
    probe = continuity_probe.Probe.__new__(continuity_probe.Probe)
    probe.r = OvernightRunner(menu_after_error="ShippingMenu")
    with pytest.raises(RuntimeError, match="Invalid menu key"):
        asyncio.run(probe.sleep())
    assert "leave-player-bed" not in probe.r.actions
