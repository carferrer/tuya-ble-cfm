"""Check that the connectivity sensor tracks the real paired BLE session."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "custom_components/tuya_ble/binary_sensor.py"
)


class FakeBase:
    def __init__(self, hass, coordinator, device, product, description, domain):
        self._device = device
        self.description = description
        self.domain = domain
        self.writes = 0

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, unsubscribe):
        pass

    def async_write_ha_state(self):
        self.writes += 1


def test_connection_sensor_reports_connect_and_disconnect_without_unavailability():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "TuyaBLEConnectionBinarySensor"
    )
    cls.bases = [ast.Name(id="FakeBase", ctx=ast.Load())]
    callbacks = []

    def register(callback):
        callbacks.append(callback)
        return lambda: callbacks.remove(callback)

    namespace = {
        "FakeBase": FakeBase,
        "callback": lambda func: func,
        "BinarySensorEntityDescription": lambda **kwargs: SimpleNamespace(**kwargs),
        "BinarySensorDeviceClass": SimpleNamespace(CONNECTIVITY="connectivity"),
        "EntityCategory": SimpleNamespace(DIAGNOSTIC="diagnostic"),
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
    device = SimpleNamespace(connected=False, register_connection_status_callback=register)
    data = SimpleNamespace(device=device, coordinator=None, product=None)
    entity = namespace["TuyaBLEConnectionBinarySensor"](None, data)
    assert entity.available is True
    assert entity._attr_is_on is False
    assert entity.description.device_class == "connectivity"
    assert entity.description.entity_category == "diagnostic"
    asyncio.run(entity.async_added_to_hass())

    device.connected = True
    callbacks[0]()
    assert entity._attr_is_on is True
    assert entity.writes == 1

    device.connected = False
    callbacks[0]()
    assert entity._attr_is_on is False
    assert entity.writes == 2
    entity._handle_coordinator_update()
    assert entity.writes == 2
