"""Verify only device-reported Boolean lock states survive a restart."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[1] / "custom_components/tuya_ble/lock_state.py"


class FakeStore:
    data = None

    def __init__(self, hass, version, key):
        self.key = key
        self.saved = []

    async def async_load(self):
        return self.data

    def async_delay_save(self, data_func, delay):
        self.saved.append(data_func())


def test_restore_and_save_only_confirmed_boolean_reports():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TuyaBLELockState"
    )
    callbacks = []

    def register(callback):
        callbacks.append(callback)
        return lambda: callbacks.remove(callback)

    namespace = {
        "Store": FakeStore,
        "LOCK_STATE_STORE_VERSION": 1,
        "STORED_DPS": (33, 47),
        "TuyaBLEDataPointType": SimpleNamespace(DT_BOOL="bool"),
        "callback": lambda func: func,
    }
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(
        compile(
            module,
            str(SOURCE),
            "exec",
            flags=__import__("__future__").annotations.compiler_flag,
        ),
        namespace,
    )
    FakeStore.data = {"values": {"33": True, "47": False, "69": True}}
    device = SimpleNamespace(register_callback=register)
    state = namespace["TuyaBLELockState"](None, "entry-one", device)
    asyncio.run(state.async_load())
    state.register()
    assert state.get(33) is True
    assert state.get(47) is False
    assert state.get(69) is None

    callbacks[0]([SimpleNamespace(id=47, type="bool", value=True)])
    assert state.get(47) is True
    assert state._store.saved[-1] == {"values": {"33": True, "47": True}}

    callbacks[0]([SimpleNamespace(id=33, type="bool", value=1)])
    callbacks[0]([SimpleNamespace(id=47, type="enum", value=False)])
    assert len(state._store.saved) == 1
    assert state.get(33) is True
    assert state.get(47) is True
    state.unregister()
    assert callbacks == []


def test_motor_binary_sensor_uses_saved_report_not_transport_cache():
    sensor_source = SOURCE.with_name("binary_sensor.py")
    tree = ast.parse(sensor_source.read_text(encoding="utf-8"))
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TuyaBLEBinarySensor"
    )
    cls.bases = [ast.Name(id="FakeBase", ctx=ast.Load())]

    class FakeBase:
        def __init__(self, hass, coordinator, device, product, description, domain):
            self._device = device
            self._attr_is_on = None

        async def async_added_to_hass(self):
            pass

        def async_write_ha_state(self):
            pass

    namespace = {"FakeBase": FakeBase, "callback": lambda func: func}
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(
        compile(
            module,
            str(sensor_source),
            "exec",
            flags=__import__("__future__").annotations.compiler_flag,
        ),
        namespace,
    )
    device = SimpleNamespace(
        datapoints={47: SimpleNamespace(value=True)},
        _cfm_lock_state=SimpleNamespace(get=lambda dp_id: False),
    )
    mapping = SimpleNamespace(dp_id=47, description=SimpleNamespace())
    entity = namespace["TuyaBLEBinarySensor"](None, None, device, None, mapping)
    asyncio.run(entity.async_added_to_hass())
    assert entity.available is True
    assert entity._attr_is_on is False
    entity._handle_coordinator_update()
    assert entity._attr_is_on is False
