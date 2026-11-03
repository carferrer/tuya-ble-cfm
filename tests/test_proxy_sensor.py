"""Track the backend that connected, rather than an unrelated BLE advert."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[1] / "custom_components/tuya_ble/sensor.py"


class FakeBase:
    def __init__(self, hass, coordinator, device, product, description):
        self._device = device
        self.description = description
        self._attr_native_value = None
        self.writes = 0

    @property
    def native_value(self):
        return self._attr_native_value

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, unsubscribe):
        pass

    def async_write_ha_state(self):
        self.writes += 1


class FakeStore:
    values = {}

    def __init__(self, hass, version, key):
        self.key = key

    def async_delay_save(self, data_factory, delay):
        self.values[self.key] = data_factory()

    async def async_load(self):
        return self.values.get(self.key)


def _proxy_sensor_class():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        and node.name in ("connected_proxy_name", "TuyaBLELastProxySensor")
    ]
    sensor_class = next(node for node in nodes if isinstance(node, ast.ClassDef))
    sensor_class.bases = [ast.Name(id="FakeBase", ctx=ast.Load())]
    namespace = {
        "FakeBase": FakeBase,
        "Store": FakeStore,
        "DOMAIN": "tuya_ble",
        "callback": lambda func: func,
        "SensorEntityDescription": lambda **kwargs: SimpleNamespace(**kwargs),
        "EntityCategory": SimpleNamespace(DIAGNOSTIC="diagnostic"),
    }
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(
        compile(
            module,
            str(SOURCE),
            "exec",
            flags=__import__("__future__").annotations.compiler_flag,
        ),
        namespace,
    )
    return namespace["TuyaBLELastProxySensor"]


def test_last_proxy_is_from_connected_backend_and_survives_disconnect_and_restart():
    FakeStore.values.clear()
    callbacks = []

    def register(callback):
        callbacks.append(callback)
        return lambda: callbacks.remove(callback)

    device = SimpleNamespace(
        connected=False,
        _client=None,
        register_connected_callback=register,
    )
    data = SimpleNamespace(device=device, coordinator=None, product=None)
    entry = SimpleNamespace(entry_id="lock-1")
    sensor_class = _proxy_sensor_class()
    sensor = sensor_class(None, entry, data)
    asyncio.run(sensor.async_added_to_hass())
    assert sensor.available is True
    assert sensor.native_value is None

    # An advertisement from another proxy must not be shown as the GATT route.
    device._ble_device = SimpleNamespace(details={"source": "wrong-advertiser"})
    device.connected = True
    device._client = SimpleNamespace(
        _backend=SimpleNamespace(_source_name="Proxy salón [F4:65:0B:40:09:66]")
    )
    callbacks[0]()
    assert sensor.native_value == "Proxy salón [F4:65:0B:40:09:66]"
    assert sensor.writes == 1

    device.connected = False
    callbacks[0]()
    assert sensor.native_value == "Proxy salón [F4:65:0B:40:09:66]"
    assert sensor.writes == 1

    restored = sensor_class(None, entry, data)
    asyncio.run(restored.async_added_to_hass())
    assert restored.native_value == "Proxy salón [F4:65:0B:40:09:66]"


def test_last_proxy_falls_back_to_backend_source_and_ignores_missing_backend():
    FakeStore.values.clear()
    sensor_class = _proxy_sensor_class()
    device = SimpleNamespace(
        connected=True,
        _client=SimpleNamespace(_backend=SimpleNamespace(_source="AA:BB:CC:DD:EE:FF")),
        register_connected_callback=lambda callback: lambda: None,
    )
    data = SimpleNamespace(device=device, coordinator=None, product=None)
    sensor = sensor_class(None, SimpleNamespace(entry_id="lock-2"), data)
    asyncio.run(sensor.async_added_to_hass())
    assert sensor.native_value == "AA:BB:CC:DD:EE:FF"

    device._client = SimpleNamespace(_backend=SimpleNamespace())
    sensor._handle_connection()
    assert sensor.native_value == "AA:BB:CC:DD:EE:FF"
