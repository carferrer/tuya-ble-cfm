"""Verify the two product-specific DP6 pulses."""

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
    def __init__(self, initial=False, fail_first=False, dp_type="bool"):
        self.value = initial
        self.type = dp_type
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
    helper = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_press_bluetooth_unlock")
    cls.bases = [ast.Name(id="FakeBase", ctx=ast.Load())]
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    namespace = {
        "FakeBase": FakeBase,
        "asyncio": SimpleNamespace(Lock=asyncio.Lock, sleep=sleep),
        "PRODUCT_B3AOULUH": "b3aouluh",
        "TuyaBLEDataPointType": SimpleNamespace(DT_BOOL="bool", DT_RAW="raw"),
        "UNLOCK_PULSE_SECONDS": 0.5,
        "OKKY_UNLOCK_PAYLOAD": b"\x01\x01",
        "HomeAssistantError": RuntimeError,
    }
    module = ast.fix_missing_locations(ast.Module(body=[helper, cls], type_ignores=[]))
    exec(compile(module, str(BUTTON), "exec", flags=__import__("__future__").annotations.compiler_flag), namespace)
    datapoint = FakeDatapoint(
        initial, fail_first, "bool" if product == "b3aouluh" else "raw"
    )
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


def test_okky_sends_two_raw_unlock_commands_half_a_second_apart():
    button, dp, delays = _button(product="okkyfgfs")
    asyncio.run(button.async_press())
    assert dp.sent == [b"\x01\x01", b"\x01\x01"]
    assert delays == [0.5]


def test_okky_rejects_a_bool_dp6_cache_instead_of_sending_wrong_type():
    button, dp, delays = _button(product="okkyfgfs")
    dp.type = "bool"
    with pytest.raises(RuntimeError, match="Expected a raw DP6"):
        asyncio.run(button.async_press())
    assert dp.sent == []
    assert delays == []


def test_failed_first_write_does_not_send_second_half():
    button, dp, delays = _button(fail_first=True)
    with pytest.raises(RuntimeError, match="BLE write failed"):
        asyncio.run(button.async_press())
    assert dp.sent == [True]
    assert delays == []


def test_okky_failed_first_write_does_not_repeat():
    button, dp, delays = _button(product="okkyfgfs", fail_first=True)
    with pytest.raises(RuntimeError, match="BLE write failed"):
        asyncio.run(button.async_press())
    assert dp.sent == [b"\x01\x01"]
    assert delays == []
