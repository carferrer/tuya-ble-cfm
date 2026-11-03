"""Verify the product-specific DP6 commands."""

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
        "PRODUCT_OKKYFGFS": "okkyfgfs",
        "PRODUCT_7A4XVBTT": "7a4xvbtt",
        "PRODUCT_8GZA4O8A": "8gza4o8a",
        "TuyaBLEDataPointType": SimpleNamespace(DT_BOOL="bool", DT_RAW="raw"),
        "UNLOCK_PULSE_SECONDS": 0.5,
        "UNLOCK_CONNECT_SETTLE_SECONDS": 0.5,
        "RAW_UNLOCK_MEMBER_1_PAYLOAD": b"\x01\x01",
        "HomeAssistantError": RuntimeError,
    }
    module = ast.fix_missing_locations(ast.Module(body=[helper, cls], type_ignores=[]))
    exec(compile(module, str(BUTTON), "exec", flags=__import__("__future__").annotations.compiler_flag), namespace)
    datapoint = FakeDatapoint(
        initial, fail_first, "bool" if product == "b3aouluh" else "raw"
    )
    datapoints = SimpleNamespace(get_or_create=lambda *args: datapoint)
    device = SimpleNamespace(product_id=product, datapoints=datapoints, connected=True)
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


def test_new_lock_sends_one_raw_unlock_command_when_connected():
    button, dp, delays = _button(product="7a4xvbtt")
    asyncio.run(button.async_press())
    assert dp.sent == [b"\x01\x01"]
    assert delays == []


def test_la_t01_dp71_button_fetches_cloud_report_and_sends_once():
    source = ast.parse(BUTTON.read_text(encoding="utf-8"))
    cls = next(
        node for node in source.body
        if isinstance(node, ast.ClassDef) and node.name == "TuyaBLEDP71TestButton"
    )
    cls.bases = [ast.Name(id="FakeBase", ctx=ast.Load())]
    events = []
    datapoint = FakeDatapoint(dp_type="raw")

    async def fetch(device_id):
        events.append(("cloud", device_id))
        return [{"code": "ble_unlock_check"}]

    async def send(value):
        events.append(("ble", value))

    async def save(value):
        events.append(("save", value))
        device._cfm_dp71_local_command = value

    async def clear():
        events.append("clear")
        device._cfm_dp71_local_command = None

    datapoint.set_value = send
    device = SimpleNamespace(
        product_id="8gza4o8a",
        device_id="device123",
        connected=True,
        datapoints=SimpleNamespace(get_or_create=lambda *args: datapoint),
        _cfm_dp71_session=SimpleNamespace(
            async_save_command=save, async_clear=clear
        ),
    )
    manager = SimpleNamespace(get_lock_unlock_properties=fetch)
    namespace = {
        "FakeBase": FakeBase,
        "asyncio": asyncio,
        "PRODUCT_8GZA4O8A": "8gza4o8a",
        "TuyaBLEDataPointType": SimpleNamespace(DT_RAW="raw"),
        "ButtonEntityDescription": lambda **kwargs: SimpleNamespace(**kwargs),
        "HomeAssistantError": RuntimeError,
        "time": SimpleNamespace(time=lambda: 1800000000),
        "build_dp71_unlock": lambda props, timestamp: b"one-fresh-command",
    }
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(
        compile(module, str(BUTTON), "exec", flags=__import__("__future__").annotations.compiler_flag),
        namespace,
    )
    button = namespace["TuyaBLEDP71TestButton"](
        None, SimpleNamespace(coordinator=None, device=device, product=None, manager=manager)
    )
    asyncio.run(button.async_press())
    assert events == [
        ("cloud", "device123"),
        ("save", b"one-fresh-command"),
        ("ble", b"one-fresh-command"),
    ]
    assert device._cfm_dp71_local_command == b"one-fresh-command"

    async def failed_send(value):
        raise RuntimeError("BLE write failed")

    datapoint.set_value = failed_send
    with pytest.raises(RuntimeError, match="BLE write failed"):
        asyncio.run(button.async_press())
    assert device._cfm_dp71_local_command is None
    assert events[-2:] == [("save", b"one-fresh-command"), "clear"]


def test_la_t01_local_button_requires_seed_then_uses_ble_without_cloud():
    source = ast.parse(BUTTON.read_text(encoding="utf-8"))
    build_command = next(
        node for node in source.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_local_dp71_command"
    )
    send_command = next(
        node for node in source.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_press_dp71_local_unlock"
    )
    cls = next(
        node for node in source.body
        if isinstance(node, ast.ClassDef) and node.name == "TuyaBLEDP71LocalTestButton"
    )
    cls.bases = [ast.Name(id="FakeBase", ctx=ast.Load())]
    events = []
    datapoint = FakeDatapoint(dp_type="raw")

    async def reconnect():
        events.append("reconnect")
        device.connected = True

    async def send(value):
        events.append(("ble", value))

    async def sleep(seconds):
        events.append(("settle", seconds))

    datapoint.set_value = send
    device = SimpleNamespace(
        product_id="8gza4o8a",
        connected=False,
        reconnect=reconnect,
        datapoints=SimpleNamespace(get_or_create=lambda *args: datapoint),
    )
    namespace = {
        "FakeBase": FakeBase,
        "asyncio": SimpleNamespace(Lock=asyncio.Lock, sleep=sleep),
        "PRODUCT_8GZA4O8A": "8gza4o8a",
        "TuyaBLEDataPointType": SimpleNamespace(DT_RAW="raw"),
        "ButtonEntityDescription": lambda **kwargs: SimpleNamespace(**kwargs),
        "HomeAssistantError": RuntimeError,
        "UNLOCK_CONNECT_SETTLE_SECONDS": 0.5,
        "time": SimpleNamespace(time=lambda: 1800000030),
        "refresh_dp71_unlock": lambda command, timestamp: b"fresh-local-command",
    }
    module = ast.fix_missing_locations(ast.Module(body=[build_command, send_command, cls], type_ignores=[]))
    exec(
        compile(module, str(BUTTON), "exec", flags=__import__("__future__").annotations.compiler_flag),
        namespace,
    )
    button = namespace["TuyaBLEDP71LocalTestButton"](
        None, SimpleNamespace(coordinator=None, device=device, product=None)
    )
    with pytest.raises(RuntimeError, match="Preparar y abrir"):
        asyncio.run(button.async_press())
    assert events == []

    device._cfm_dp71_local_command = b"validated-cloud-command"
    asyncio.run(button.async_press())
    assert events == ["reconnect", ("settle", 0.5), ("ble", b"fresh-local-command")]
    assert device._cfm_dp71_local_command == b"validated-cloud-command"


def test_new_lock_connects_and_waits_before_one_unlock_write():
    button, dp, delays = _button(product="7a4xvbtt")
    device = button._device
    device.connected = False
    calls = []

    async def reconnect():
        calls.append("reconnect")
        device.connected = True

    async def send(value):
        assert calls == ["reconnect"]
        assert delays == [0.5]
        await FakeDatapoint.set_value(dp, value)

    device.reconnect = reconnect
    dp.set_value = send
    asyncio.run(button.async_press())
    assert dp.sent == [b"\x01\x01"]
    assert delays == [0.5]


def test_new_lock_does_not_write_when_pairing_is_incomplete():
    button, dp, delays = _button(product="7a4xvbtt")
    button._device.connected = False

    async def reconnect():
        pass

    button._device.reconnect = reconnect
    with pytest.raises(RuntimeError, match="paired BLE connection"):
        asyncio.run(button.async_press())
    assert dp.sent == []
    assert delays == []


def test_okky_rejects_a_bool_dp6_cache_instead_of_sending_wrong_type():
    button, dp, delays = _button(product="okkyfgfs")
    dp.type = "bool"
    with pytest.raises(RuntimeError, match="Expected a raw DP6"):
        asyncio.run(button.async_press())
    assert dp.sent == []
    assert delays == []


def test_new_lock_rejects_non_raw_dp6_before_write():
    button, dp, delays = _button(product="7a4xvbtt")
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


def test_new_lock_failed_write_does_not_retry():
    button, dp, delays = _button(product="7a4xvbtt", fail_first=True)
    with pytest.raises(RuntimeError, match="BLE write failed"):
        asyncio.run(button.async_press())
    assert dp.sent == [b"\x01\x01"]
    assert delays == []


def test_okky_failed_first_write_does_not_repeat():
    button, dp, delays = _button(product="okkyfgfs", fail_first=True)
    with pytest.raises(RuntimeError, match="BLE write failed"):
        asyncio.run(button.async_press())
    assert dp.sent == [b"\x01\x01"]
    assert delays == []
