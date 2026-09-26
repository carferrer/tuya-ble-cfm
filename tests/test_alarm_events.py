"""Exercise real alarm helpers/lifecycle with lightweight HA boundary doubles."""

from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble"
EXPECTED = [
    "wrong_finger",
    "wrong_password",
    "wrong_card",
    "wrong_face",
    "tongue_bad",
    "too_hot",
    "unclosed_time",
    "tongue_not_out",
    "pry",
    "key_in",
    "low_battery",
    "power_off",
    "shock",
]


class DPType(Enum):
    DT_ENUM = 4
    DT_VALUE = 2


class FakeEvent:
    async def async_added_to_hass(self):
        self.emitted = []
        self.removers = []

    def async_on_remove(self, callback):
        self.removers.append(callback)

    def _trigger_event(self, kind, data):
        assert kind in self._attr_event_types
        self.emitted.append((kind, data))

    def async_write_ha_state(self):
        pass


class FakeStore:
    def __init__(self, hass, version, key):
        self.db = hass
        self.key = key
        self.on_load = None
        self.pending = None

    async def async_load(self):
        if self.on_load:
            self.on_load()
        return deepcopy(self.db.get(self.key))

    async def async_save(self, data):
        self.db[self.key] = deepcopy(data)

    def async_delay_save(self, callback, delay):
        self.pending = callback

    async def flush(self):
        if self.pending:
            await self.async_save(self.pending())


def load_code(filename, namespace):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    tree.body = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.ImportFrom)
            and (node.level or (node.module or "").startswith("homeassistant"))
        )
    ]
    exec(compile(tree, str(ROOT / filename), "exec"), namespace)


@pytest.fixture
def code():
    ns = {
        "DOMAIN": "tuya_ble",
        "TuyaBLEDataPointType": DPType,
        "EventEntity": FakeEvent,
        "Store": FakeStore,
        "callback": lambda fn: fn,
        "get_device_info": lambda device: {},
    }
    load_code("alarm.py", ns)
    load_code("alarm_event.py", ns)
    return SimpleNamespace(**ns)


def dp(value, timestamp=1790367222, dp_id=21, dp_type=DPType.DT_ENUM):
    return SimpleNamespace(id=dp_id, value=value, timestamp=timestamp, type=dp_type)


def history(value, timestamp, received=1790367340):
    return {
        "received_at": received,
        "datapoints": [
            {"id": 21, "type": "DT_ENUM", "value": value, "timestamp": timestamp}
        ],
    }


def entity(code, db=None, entry_id="lock1", records=None):
    device = SimpleNamespace(device_id=entry_id, _cfm_received_dp_events=records or [])

    def register(callback):
        device.listener = callback
        return lambda: setattr(device, "listener", None)

    device.register_callback = register
    return code.TuyaBLEAlarmEvent(
        db if db is not None else {}, SimpleNamespace(entry_id=entry_id), device
    )


@pytest.mark.parametrize("value,kind", list(enumerate(EXPECTED)))
def test_every_declared_alarm_retains_original_time_and_value(code, value, kind):
    record = code.alarm_record_from_datapoint(dp(value), 1790367340)
    assert record["event_type"] == kind
    assert record["event_type_id"] == 2100 + value
    assert record["alarm_value"] == value
    assert record["event_time"] == "2026-09-25T20:13:42+00:00"
    assert record["received_at"] == "2026-09-25T20:15:40+00:00"
    assert record["delay_seconds"] == 118
    assert record["recovered"] is True
    assert "access_value" not in record and "member_id" not in record


@pytest.mark.parametrize("value", [-1, 13, 255, True, "0", None])
def test_invalid_enums_are_ignored(code, value):
    assert code.alarm_record_from_datapoint(dp(value)) is None


@pytest.mark.parametrize("stamp", [None, "123", True, float("nan"), float("inf"), 1e30])
def test_invalid_timestamps_are_ignored(code, stamp):
    assert code.alarm_record_from_datapoint(dp(0, stamp)) is None


def test_non_alarm_and_wrong_type_are_ignored(code):
    assert code.alarm_record_from_datapoint(dp(0, dp_id=12)) is None
    assert code.alarm_record_from_datapoint(dp(0, dp_type=DPType.DT_VALUE)) is None
    assert list(code.iter_alarm_records_from_history([history(0, None)])) == []


def test_live_alarm_is_not_marked_recovered(code):
    assert code.build_alarm_record(0, 100, 101)["recovered"] is False


def test_all_thirteen_alarm_types_can_be_emitted(code):
    alarm = entity(code)
    asyncio.run(alarm.async_added_to_hass())
    alarm._handle_updates([dp(value) for value in range(13)])
    assert [kind for kind, _ in alarm.emitted] == EXPECTED
    assert [data["alarm_value"] for _, data in alarm.emitted] == list(range(13))
    assert [data["event_type_id"] for _, data in alarm.emitted] == list(range(2100, 2113))


@pytest.mark.parametrize("dp_id", [12, 13, 14, 15, 19, 55, 62, 63])
def test_access_type_id_is_additive_and_does_not_change_deduplication(code, dp_id):
    ns = vars(code).copy()
    ns["PRODUCT_B3AOULUH"] = "b3aouluh"
    ns["EVENT_PASSAGE_MODE_ENABLED"] = "passage_mode_enabled"
    load_code("access.py", ns)
    load_code("event.py", ns)
    record = ns["build_access_record"](dp_id, 200, 100, 110)
    assert record["event_type_id"] == dp_id
    legacy_record = {key: value for key, value in record.items() if key != "event_type_id"}
    assert ns["access_record_key"](legacy_record) == ns["access_record_key"](record)
    device = SimpleNamespace(device_id="lock1", product_id="b3aouluh")
    access = ns["TuyaBLEAccessEvent"]({}, SimpleNamespace(entry_id="lock1"), device)
    access.emitted = []
    access._process_record(record)
    assert access.emitted[0][1]["event_type_id"] == dp_id
    assert access.emitted[0][1]["access_value"] == 200
    assert access.emitted[0][1]["member_id"] == 200
    access._process_record(legacy_record)


def test_okky_access_event_emits_live_fingerprint_without_passage_mode(code):
    ns = vars(code).copy()
    ns["PRODUCT_B3AOULUH"] = "b3aouluh"
    ns["EVENT_PASSAGE_MODE_ENABLED"] = "passage_mode_enabled"
    load_code("access.py", ns)
    load_code("event.py", ns)
    device = SimpleNamespace(
        device_id="okky-lock",
        product_id="okkyfgfs",
        datapoints={33: None},
        _cfm_received_dp_events=[],
        register_callback=lambda callback: lambda: None,
    )
    access = ns["TuyaBLEAccessEvent"](
        {}, SimpleNamespace(entry_id="okky-lock"), device
    )
    asyncio.run(access.async_added_to_hass())
    assert "passage_mode_enabled" not in access._attr_event_types
    report = dp(100, dp_id=12, dp_type=DPType.DT_VALUE)
    access._handle_updates([report, report])
    assert len(access.emitted) == 1
    assert access.emitted[0][0] == "fingerprint_unlock"
    assert access.emitted[0][1]["event_type_id"] == 12
    assert len(access.emitted) == 1


def test_repeated_failures_emit_individually_and_replays_do_not(code):
    alarm = entity(code)
    asyncio.run(alarm.async_added_to_hass())
    alarm._handle_updates([dp(0), dp(0, 1790367223), dp(1, 1790367242), dp(0)])
    assert [kind for kind, _ in alarm.emitted] == [
        "wrong_finger",
        "wrong_finger",
        "wrong_password",
    ]
    assert len({data["event_time"] for _, data in alarm.emitted}) == 3
    assert len(alarm._seen_set) == 3
    alarm.removers[0]()
    assert alarm._device.listener is None


def test_first_install_baselines_history_and_processes_startup_queue(code):
    alarm = entity(code, records=[history(0, 100)])
    alarm._store.on_load = lambda: alarm._device.listener([dp(0, 100), dp(1, 101)])
    asyncio.run(alarm.async_added_to_hass())
    assert [kind for kind, _ in alarm.emitted] == ["wrong_password"]


def test_reload_suppresses_saved_records_but_emits_unseen_history(code):
    db = {}
    first = entity(code, db)
    asyncio.run(first.async_added_to_hass())
    first._handle_updates([dp(0, 100)])
    asyncio.run(first._store.flush())
    second = entity(
        code, db, records=[history(1, 102), history(0, 101), history(0, 100)]
    )
    asyncio.run(second.async_added_to_hass())
    assert [kind for kind, _ in second.emitted] == ["wrong_password", "wrong_finger"]
    assert second._last_record["event_timestamp"] == 102
    assert second._store.key == "tuya_ble.alarm_records.lock1"
    other = entity(code, db, entry_id="lock2")
    asyncio.run(other.async_added_to_hass())
    other._handle_updates([dp(0, 100)])
    assert len(other.emitted) == 1


def test_dedup_history_is_bounded(code):
    alarm = entity(code)
    asyncio.run(alarm.async_added_to_hass())
    alarm._handle_updates([dp(0, stamp) for stamp in range(201)])
    assert len(alarm.emitted) == 201
    assert len(alarm._seen_keys) == len(alarm._seen_set) == 200


def test_setup_only_adds_alarm_for_validated_product(code):
    ns = vars(code).copy()
    ns.update(
        {
            "setup_connection_policy": lambda *args: None,
                "PRODUCT_B3AOULUH": "b3aouluh",
                "PRODUCT_OKKYFGFS": "okkyfgfs",
        }
    )
    # Load the real platform setup with an inert access entity constructor.
    tree = ast.parse((ROOT / "event.py").read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)]
    ns["TuyaBLEAccessEvent"] = lambda *args: "access"
    exec(
        compile(
            tree,
            "event.py",
            "exec",
            flags=__import__("__future__").annotations.compiler_flag,
        ),
        ns,
    )
    for product, expected in [("b3aouluh", 2), ("okkyfgfs", 2)]:
        added = []
        device = SimpleNamespace(product_id=product, device_id="lock1")
        entry = SimpleNamespace(
            entry_id="lock1",
            async_on_unload=lambda fn: None,
            add_update_listener=lambda fn: None,
        )
        hass = SimpleNamespace(
            data={"tuya_ble": {"lock1": SimpleNamespace(device=device)}}
        )
        asyncio.run(ns["async_setup_entry"](hass, entry, added.extend))
        assert len(added) == expected
        if added:
            assert added[0] == "access"
            assert added[1]._attr_unique_id == "lock1-alarm-events"
