"""Exercise the opt-in DP33 command against BLE and Home Assistant doubles."""

from __future__ import annotations

import ast
import asyncio
import logging
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble"


class DPType(Enum):
    DT_BOOL = 1
    DT_ENUM = 4


class HAError(Exception):
    pass


class FakeBase:
    def __init__(self, hass, coordinator, device, product, description, domain):
        self._device = device
        self.entity_description = description
        self._attr_unique_id = f"{device.device_id}-{description.key}"
        self.domain = domain
        self.remove_callbacks = []
        self.writes = 0

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, callback):
        self.remove_callbacks.append(callback)

    def async_write_ha_state(self):
        self.writes += 1


class FakeDP:
    def __init__(self, device, value=False, dp_type=DPType.DT_BOOL):
        self.device = device
        self.id = 33
        self.type = dp_type
        self.value = value
        self.sent = []
        self.on_send = None

    async def set_value(self, value):
        # Mirror production transport: change cache before any device report.
        self.value = value
        self.sent.append(value)
        if self.on_send:
            await self.on_send(value)


class FakeDatapoints(dict):
    def has_id(self, dp_id, dp_type):
        return dp_id in self and self[dp_id].type == dp_type


class FakeDevice:
    def __init__(self, product="b3aouluh", initial=False):
        self.product_id = product
        self.device_id = "test-lock"
        self.address = "test-address"
        self.datapoints = FakeDatapoints()
        self.datapoints[33] = FakeDP(self, initial)
        self.callbacks = []

    def register_callback(self, callback):
        self.callbacks.append(callback)
        return lambda: self.callbacks.remove(callback)

    def report(self, value):
        dp = SimpleNamespace(id=33, type=DPType.DT_BOOL, value=value)
        self.datapoints[33].value = value
        for callback in list(self.callbacks):
            callback([dp])


@pytest.fixture
def code():
    tree = ast.parse((ROOT / "switch.py").read_text(encoding="utf-8"))
    tree.body = [
        node for node in tree.body
        if not isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    ns = {
        "__name__": "passage_test", "asyncio": asyncio, "logging": logging,
        "TuyaBLEEntity": FakeBase, "SwitchEntity": object,
        "SwitchEntityDescription": lambda **kw: SimpleNamespace(**kw),
        "TuyaBLEDataPointType": DPType, "HomeAssistantError": HAError,
        "callback": lambda fn: fn, "DOMAIN": "tuya_ble",
        "PRODUCT_B3AOULUH": "b3aouluh", "_LOGGER": SimpleNamespace(warning=lambda *a: None),
        "er": SimpleNamespace(
            async_get=lambda hass: SimpleNamespace(async_get_entity_id=lambda *args: None),
            RegistryEntryDisabler=SimpleNamespace(INTEGRATION="integration"),
        ),
    }
    exec(compile(tree, "switch.py", "exec", flags=__import__("__future__").annotations.compiler_flag), ns)
    return SimpleNamespace(**ns)


def make_switch(code, device):
    data = SimpleNamespace(device=device, product=None, coordinator=None)
    switch = code.TuyaBLEPassageModeSwitch(None, data)
    asyncio.run(switch.async_added_to_hass())
    return switch


def test_default_enabled_and_status_from_device_report(code):
    device = FakeDevice()
    switch = make_switch(code, device)
    assert switch._attr_entity_registry_enabled_default is True
    assert switch.entity_description.name == "Modo paso libre"
    assert switch._attr_unique_id == "test-lock-passage_mode_experimental"
    assert switch.available and switch._attr_is_on is False
    device.report(True)
    assert switch._attr_is_on is True
    device.report(False)
    assert switch._attr_is_on is False
    assert switch.writes == 2
    switch.remove_callbacks[0]()
    assert device.callbacks == []


def test_restored_passage_state_wins_over_provisional_transport_cache(code):
    device = FakeDevice(initial=True)
    device._cfm_lock_state = SimpleNamespace(get=lambda dp_id: False)
    switch = make_switch(code, device)
    assert switch.available is True
    assert switch._attr_is_on is False
    device.report(True)
    assert switch._attr_is_on is True


def test_command_waits_for_device_confirmation_and_can_reverse(code):
    device = FakeDevice()
    switch = make_switch(code, device)

    async def report(value):
        assert switch._attr_is_on is not value  # optimistic cache must not set UI state
        device.report(value)

    device.datapoints[33].on_send = report
    asyncio.run(switch.async_turn_on())
    assert switch._attr_is_on is True
    asyncio.run(switch.async_turn_off())
    assert switch._attr_is_on is False
    assert device.datapoints[33].sent == [True, False]
    assert switch._confirmation is None


def test_unconfirmed_command_fails_without_optimistic_state(code):
    device = FakeDevice()
    switch = make_switch(code, device)
    switch_code = type(switch)._set_mode.__globals__
    switch_code["CONFIRM_TIMEOUT_SECONDS"] = 0.001
    with pytest.raises(HAError, match="No DP33 confirmation"):
        asyncio.run(switch.async_turn_on())
    assert device.datapoints[33].sent == [True]
    assert switch._attr_is_on is False
    assert switch._confirmation is None


def test_wrong_product_or_wrong_dp_type_never_writes(code):
    other = FakeDevice(product="okkyfgfs")
    switch = make_switch(code, other)
    with pytest.raises(HAError, match="limited"):
        asyncio.run(switch.async_turn_on())
    assert other.datapoints[33].sent == []

    b3 = FakeDevice()
    b3.datapoints[33].type = DPType.DT_ENUM
    switch = make_switch(code, b3)
    assert not switch.available
    with pytest.raises(HAError, match="Boolean DP33"):
        asyncio.run(switch.async_turn_on())
    assert b3.datapoints[33].sent == []


def test_setup_only_adds_control_for_b3(code):
    for product, expected in [("b3aouluh", 1), ("okkyfgfs", 0)]:
        device = FakeDevice(product=product)
        data = SimpleNamespace(device=device, product=None, coordinator=None)
        hass = SimpleNamespace(data={"tuya_ble": {"entry": data}})
        added = []
        asyncio.run(code.async_setup_entry(
            hass, SimpleNamespace(entry_id="entry"), added.extend
        ))
        assert len(added) == expected


@pytest.mark.parametrize("disabled_by,should_enable", [("integration", True), ("user", False)])
def test_previous_default_disabled_entity_is_enabled_but_user_choice_is_kept(code, disabled_by, should_enable):
    updates = []
    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, platform, unique_id: "switch.lock_passage_mode",
        async_get=lambda entity_id: SimpleNamespace(disabled_by=disabled_by),
        async_update_entity=lambda entity_id, **changes: updates.append((entity_id, changes)),
    )
    code.er.async_get = lambda hass: registry
    data = SimpleNamespace(device=FakeDevice(), product=None, coordinator=None)
    hass = SimpleNamespace(data={"tuya_ble": {"entry": data}})
    asyncio.run(code.async_setup_entry(hass, SimpleNamespace(entry_id="entry"), lambda entities: None))
    assert bool(updates) is should_enable
    if should_enable:
        assert updates == [("switch.lock_passage_mode", {"disabled_by": None})]


def test_motor_lock_platform_loads_for_both_families_and_switch_only_for_b3():
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_platforms_for_device"
    )
    class Platform(Enum):
        BUTTON = 1
        SENSOR = 2
        SWITCH = 3
        LOCK = 4

    ns = {
        "Platform": Platform,
        "PLATFORMS": [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH, Platform.LOCK],
        "PRODUCT_B3AOULUH": "b3aouluh",
        "PRODUCT_OKKYFGFS": "okkyfgfs",
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "__init__.py", "exec",
                 flags=__import__("__future__").annotations.compiler_flag), ns)
    select = ns["_platforms_for_device"]
    assert select(SimpleNamespace(product_id="b3aouluh")) == ns["PLATFORMS"]
    assert select(SimpleNamespace(product_id="okkyfgfs")) == [
        Platform.BUTTON, Platform.SENSOR, Platform.LOCK
    ]
    assert select(SimpleNamespace(product_id="unsupported")) == [
        Platform.BUTTON, Platform.SENSOR
    ]
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "entry, _platforms_for_device(device)" in source
    assert "entry, _platforms_for_device(data.device)" in source
