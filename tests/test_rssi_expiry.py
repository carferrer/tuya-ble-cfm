"""Periodic RSSI expires without requiring another Bluetooth event."""
import ast
import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/tuya_ble/sensor.py"


def make_sensor_class():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TuyaBLEPeriodicRSSISensor")
    class Base:
        _attr_native_value = None
        def __init__(self, hass, coordinator, device, product, mapping):
            self.hass = hass
            self._device = device
            self.writes = 0
            self.removers = []
        async def async_added_to_hass(self):
            pass
        def async_write_ha_state(self):
            self.writes += 1
        def async_on_remove(self, fn):
            self.removers.append(fn)
    cache = {}
    clock = [1000]
    timers = []
    def schedule(hass, callback, interval):
        assert interval == timedelta(seconds=5)
        timers.append(callback)
        return lambda: timers.remove(callback)
    def last_info(hass, address, connectable):
        assert connectable is False
        return cache.get(address)
    ns = dict(TuyaBLESensor=Base, rssi_mapping=None,
              sync_interval_minutes=lambda entry: entry.interval,
              callback=lambda fn: fn, timedelta=timedelta,
              async_track_time_interval=schedule,
              monotonic_time_coarse=lambda: clock[0],
              bluetooth=SimpleNamespace(async_last_service_info=last_info))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), ns)
    return ns["TuyaBLEPeriodicRSSISensor"], cache, clock, timers


def test_expiry_recovery_and_independent_locks():
    cls, cache, clock, timers = make_sensor_class()
    a = cls(None, SimpleNamespace(interval=1), None, SimpleNamespace(address="A"), None)
    b = cls(None, SimpleNamespace(interval=5), None, SimpleNamespace(address="B"), None)
    cache.update(A=SimpleNamespace(time=1000, rssi=-70), B=SimpleNamespace(time=1000, rssi=-80))
    asyncio.run(a.async_added_to_hass())
    asyncio.run(b.async_added_to_hass())
    assert a._attr_native_value == -70 and b._attr_native_value == -80
    clock[0] = 1060
    for timer in timers:
        timer(None)
    assert a.available and a._attr_native_value is None
    assert b._attr_native_value == -80
    # A repeated payload still carries a new timestamp and restores the RSSI.
    cache["A"].time = 1060
    timers[0](None)
    assert a._attr_native_value == -70
    a.removers[0]()
    assert len(timers) == 1


def test_missing_and_stale_startup_do_not_refresh_age():
    cls, cache, clock, timers = make_sensor_class()
    sensor = cls(None, SimpleNamespace(interval=1), None, SimpleNamespace(address="A"), None)
    asyncio.run(sensor.async_added_to_hass())
    assert sensor.available and sensor._attr_native_value is None
    cache["A"] = SimpleNamespace(time=900, rssi=-60)
    sensor._handle_coordinator_update()
    assert sensor._attr_native_value is None
    cache["A"].time = 999
    sensor._handle_coordinator_update()
    assert sensor._attr_native_value == -60
    clock[0] = 1060
    sensor._handle_coordinator_update()
    assert sensor._attr_native_value is None
