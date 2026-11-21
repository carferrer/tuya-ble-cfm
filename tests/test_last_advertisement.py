"""Last advertisement timestamps are independent of GATT and survive restart."""
import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/tuya_ble/sensor.py"


def load_sensor():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TuyaBLELastAdvertisementSensor")
    class Base:
        def __init__(self, hass, coordinator, device, product, description):
            self.hass = hass
            self._device = device
            self.description = description
            self.restored = None
            self.writes = 0
            self.removers = []
        async def async_added_to_hass(self):
            pass
        async def async_get_last_sensor_data(self):
            return self.restored
        def async_write_ha_state(self):
            self.writes += 1
        def async_on_remove(self, fn):
            self.removers.append(fn)
    cache, timers = {}, []
    clock = [1000]
    def last_info(hass, address, connectable):
        assert not connectable
        return cache.get(address)
    def schedule(hass, callback, interval):
        assert interval == timedelta(seconds=5)
        timers.append(callback)
        return lambda: timers.remove(callback)
    ns = dict(TuyaBLEEntity=Base, RestoreSensor=type("Restore", (), {}),
              HomeAssistant=object, TuyaBLEData=object,
              SensorEntityDescription=lambda **kw: SimpleNamespace(**kw),
              SensorDeviceClass=SimpleNamespace(TIMESTAMP="timestamp"),
              EntityCategory=SimpleNamespace(DIAGNOSTIC="diagnostic"),
              callback=lambda fn: fn, datetime=datetime, UTC=UTC, timedelta=timedelta,
              async_track_time_interval=schedule, monotonic_time_coarse=lambda: clock[0],
              bluetooth=SimpleNamespace(async_last_service_info=last_info))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), ns)
    def make(address):
        data = SimpleNamespace(coordinator=None, device=SimpleNamespace(address=address), product=None)
        return ns["TuyaBLELastAdvertisementSensor"](None, data)
    return make, cache, clock, timers


def test_reception_time_repeated_ads_and_device_isolation():
    make, cache, clock, timers = load_sensor()
    a, b = make("A"), make("B")
    cache["A"] = SimpleNamespace(time=880)
    asyncio.run(a.async_added_to_hass())
    asyncio.run(b.async_added_to_hass())
    assert a.available and b.available and b._attr_native_value is None
    assert abs((datetime.now(UTC) - a._attr_native_value).total_seconds() - 120) < 2
    first = a._attr_native_value
    clock[0] += 20
    timers[0](None)
    assert a._attr_native_value == first  # Cached data must not look newer.
    cache["A"].time = clock[0]
    timers[0](None)
    assert a._attr_native_value > first
    assert b._attr_native_value is None
    a.removers[0]()
    assert len(timers) == 1


def test_restore_without_ads_and_ignore_older_cached_report():
    make, cache, clock, timers = load_sensor()
    sensor = make("A")
    saved = datetime.now(UTC) - timedelta(seconds=60)
    sensor.restored = SimpleNamespace(native_value=saved.isoformat())
    asyncio.run(sensor.async_added_to_hass())
    assert sensor._attr_native_value == saved
    cache["A"] = SimpleNamespace(time=880)
    timers[0](None)
    assert sensor._attr_native_value == saved
    cache["A"].time = 1000
    timers[0](None)
    assert sensor._attr_native_value > saved


def test_invalid_restore_stays_unknown_until_an_advertisement():
    make, cache, clock, timers = load_sensor()
    sensor = make("A")
    sensor.restored = SimpleNamespace(native_value="unknown")
    asyncio.run(sensor.async_added_to_hass())
    assert sensor._attr_native_value is None
    cache["A"] = SimpleNamespace(time=1000)
    timers[0](None)
    assert sensor._attr_native_value.tzinfo is not None
