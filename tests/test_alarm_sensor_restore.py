"""Verify last-known alarm restoration and precedence of fresh DP21 reports."""

from __future__ import annotations

import ast
import asyncio
from enum import Enum
from pathlib import Path
from types import SimpleNamespace


SENSOR = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble" / "sensor.py"


class DPType(Enum):
    DT_ENUM = 4
    DT_VALUE = 2


def _alarm_sensor_class():
    tree = ast.parse(SENSOR.read_text(encoding="utf-8"))
    generic = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TuyaBLESensor")
    alarm = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TuyaBLEAlarmSensor")
    generic.body = [node for node in generic.body if isinstance(node, ast.FunctionDef) and node.name == "_handle_coordinator_update"]
    generic.bases = []
    generic.body[0].decorator_list = []
    for node in alarm.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.decorator_list = [
                decorator
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Name) and decorator.id == "property"
            ]

    class FakeBase:
        def __init__(self, hass, coordinator, device, product, description):
            self._coordinator = coordinator
            self._device = device
            self.entity_description = description
            self.writes = 0

        async def async_added_to_hass(self):
            pass

        def async_write_ha_state(self):
            self.writes += 1

    class FakeRestoreSensor:
        @property
        def native_value(self):
            return getattr(self, "_attr_native_value", None)

        async def async_get_last_sensor_data(self):
            return self._restored_data

        async def async_get_last_state(self):
            return self._restored_state

    class FakeStore:
        def __init__(self, hass, version, key):
            self.db = hass
            self.key = key

        async def async_load(self):
            return self.db.get(self.key)

        def async_delay_save(self, callback, delay):
            self.db[self.key] = callback()

    ns = {
        "TuyaBLEEntity": FakeBase,
        "RestoreSensor": FakeRestoreSensor,
        "Store": FakeStore,
        "TuyaBLEDataPointType": DPType,
        "ALARM_OPTIONS": ["wrong_finger", "wrong_password"],
        "ALARM_SENSOR_STORE_VERSION": 1,
        "ALARM_STORE_VERSION": 1,
        "alarm_sensor_store_key": lambda entry_id: f"sensor.{entry_id}",
        "alarm_store_key": lambda entry_id: f"events.{entry_id}",
    }
    module = ast.fix_missing_locations(ast.Module(body=[generic, alarm], type_ignores=[]))
    exec(compile(module, str(SENSOR), "exec", flags=__import__("__future__").annotations.compiler_flag), ns)
    return ns["TuyaBLEAlarmSensor"]


def _sensor(current_dp=None, restored="wrong_finger", legacy_state=None, db=None):
    cls = _alarm_sensor_class()
    coordinator = SimpleNamespace(connected=False)
    device = SimpleNamespace(datapoints={21: current_dp})
    mapping = SimpleNamespace(dp_id=21, coefficient=1, getter=None, description=SimpleNamespace(options=["wrong_finger", "wrong_password"]))
    db = {} if db is None else db
    sensor = cls(db, SimpleNamespace(entry_id="entry"), coordinator, device, None, mapping)
    sensor._restored_data = None if restored is None else SimpleNamespace(native_value=restored)
    sensor._restored_state = None if legacy_state is None else SimpleNamespace(state=legacy_state)
    return sensor, coordinator, device


def test_alarm_last_value_survives_restart_without_ble_connection():
    sensor, coordinator, device = _sensor()
    asyncio.run(sensor.async_added_to_hass())
    assert sensor.native_value == "wrong_finger"
    assert sensor.available is True

    device.datapoints[21] = SimpleNamespace(type=DPType.DT_ENUM, value=1)
    sensor._handle_coordinator_update()
    assert sensor.native_value == "wrong_password"


def test_fresh_report_wins_over_restored_value():
    sensor, coordinator, device = _sensor(
        current_dp=SimpleNamespace(type=DPType.DT_ENUM, value=1)
    )
    asyncio.run(sensor.async_added_to_hass())
    assert sensor.native_value == "wrong_password"


def test_invalid_or_absent_restored_alarm_is_ignored():
    for restored in (None, "unknown", "unexpected"):
        sensor, coordinator, device = _sensor(restored=restored)
        asyncio.run(sensor.async_added_to_hass())
        assert sensor.native_value is None
        assert sensor.available is False


def test_first_upgrade_restores_previous_visible_state():
    sensor, coordinator, device = _sensor(restored=None, legacy_state="wrong_password")
    asyncio.run(sensor.async_added_to_hass())
    assert sensor.native_value == "wrong_password"


def test_alarm_event_store_recovers_value_when_ha_restore_is_unknown():
    db = {"events.entry": {"last_record": {"event_type": "wrong_password"}}}
    sensor, coordinator, device = _sensor(restored=None, legacy_state="unknown", db=db)
    asyncio.run(sensor.async_added_to_hass())
    assert sensor.native_value == "wrong_password"
    assert db["sensor.entry"] == {"value": "wrong_password"}


def test_sensor_store_survives_subsequent_restart_without_ha_restore():
    db = {}
    sensor, coordinator, device = _sensor(restored=None, db=db)
    device.datapoints[21] = SimpleNamespace(type=DPType.DT_ENUM, value=1)
    sensor._handle_coordinator_update()
    assert db["sensor.entry"] == {"value": "wrong_password"}

    next_sensor, coordinator, device = _sensor(restored=None, db=db)
    asyncio.run(next_sensor.async_added_to_hass())
    assert next_sensor.native_value == "wrong_password"
