"""Verify that one HA button press sends the proven b3 DP6 pulse."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


BUTTON = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble" / "button.py"


class FakeBase:
    def __init__(self, hass, coordinator, device, product, description, domain):
        self._device = device


class FakeDatapoint:
    def __init__(self, initial=False, fail_first=False):
        self.value = initial
        self.sent = []
        self.fail_first = fail_first

    async def set_value(self, value):
        self.value = value
        self.sent.append(value)
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("BLE write failed")


def _button(product="b3aouluh", initial=False, fail_first=False):
    source = ast.parse(BUTTON.read_text(encoding="utf-8"))
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "TuyaBLEButton")
    cls.bases = [ast.Name(id="FakeBase", ctx=ast.Load())]
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    namespace = {
        "FakeBase": FakeBase,
        "asyncio": SimpleNamespace(Lock=asyncio.Lock, sleep=sleep),
        "PRODUCT_B3AOULUH": "b3aouluh",
        "TuyaBLEDataPointType": SimpleNamespace(DT_BOOL="bool"),
        "UNLOCK_PULSE_SECONDS": 0.5,
    }
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(compile(module, str(BUTTON), "exec", flags=__import__("__future__").annotations.compiler_flag), namespace)
    datapoint = FakeDatapoint(initial, fail_first)
    datapoints = SimpleNamespace(get_or_create=lambda *args: datapoint)
    device = SimpleNamespace(product_id=product, datapoints=datapoints)
    button = namespace["TuyaBLEButton"](
        None, None, device, None, SimpleNamespace(dp_id=6, description=SimpleNamespace())
    )
    return button, datapoint, delays


def test_single_press_sends_true_then_false_and_repeats_same_pulse():
    button, dp, delays = _button()
    assert button.available is True
    asyncio.run(button.async_press())
    asyncio.run(button.async_press())
    assert dp.sent == [True, False, True, False]
    assert delays == [0.5, 0.5]


def test_other_lock_keeps_single_dp6_write():
    button, dp, delays = _button(product="okkyfgfs")
    asyncio.run(button.async_press())
    assert dp.sent == [True]
    assert delays == []


def test_failed_first_write_does_not_send_second_half():
    button, dp, delays = _button(fail_first=True)
    with pytest.raises(RuntimeError, match="BLE write failed"):
        asyncio.run(button.async_press())
    assert dp.sent == [True]
    assert delays == []
