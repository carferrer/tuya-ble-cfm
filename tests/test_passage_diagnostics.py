"""Check passage notifications and per-lock diagnostics without HA runtime."""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble"


def _method(filename: str, class_name: str, method_name: str, namespace: dict):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name)
    method.decorator_list = []
    exec(compile(ast.Module(body=[method], type_ignores=[]), filename, "exec"), namespace)
    return namespace[method_name]


def test_passage_event_only_on_observed_closed_to_open_transition():
    dp_type = SimpleNamespace(DT_BOOL="bool")
    namespace = {
        "PRODUCT_B3AOULUH": "b3aouluh",
        "DP_PASSAGE_MODE": 33,
        "EVENT_PASSAGE_MODE_ENABLED": "passage_mode_enabled",
        "TuyaBLEDataPointType": dp_type,
        "datetime": datetime,
        "UTC": UTC,
        "time": SimpleNamespace(time=lambda: 1790367222.0),
        "access_record_from_datapoint": lambda dp: None,
    }
    emit = _method("event.py", "TuyaBLEAccessEvent", "_emit_passage_mode_enabled", namespace)
    handle = _method("event.py", "TuyaBLEAccessEvent", "_handle_updates", namespace)
    events = []
    entity = SimpleNamespace(
        _device=SimpleNamespace(product_id="b3aouluh"),
        _passage_mode=None,
        _ready=True,
        _pending_passage_open=False,
        _trigger_event=lambda kind, data: events.append((kind, data)),
        async_write_ha_state=lambda: None,
        _process_record=lambda record: None,
    )
    entity._emit_passage_mode_enabled = lambda: emit(entity)
    entity._handle_updates = lambda updates: handle(entity, updates)
    report = lambda value: SimpleNamespace(id=33, type="bool", value=value)

    entity._handle_updates([report(True), report(True), report(False)])
    assert events == []  # An already-open startup state is only a baseline.
    entity._handle_updates([report(True), report(True), report(False)])
    assert len(events) == 1
    kind, data = events[0]
    assert kind == "passage_mode_enabled"
    assert data["event_type_id"] == 33
    assert data["method"] == "passage_mode"


def test_last_connection_sensor_uses_actual_connection_callback():
    namespace = {"time": SimpleNamespace(time=lambda: 1790367222.0)}
    connect = _method("devices.py", "TuyaBLECoordinator", "_async_handle_connect", namespace)
    listener_updates = []
    coordinator = SimpleNamespace(
        _device=SimpleNamespace(connected=True),
        _unsub_disconnect=None,
        _disconnected=True,
        last_connected_at=None,
        async_update_listeners=lambda: listener_updates.append(True),
    )
    connect(coordinator)
    assert coordinator.last_connected_at == 1790367222.0
    assert listener_updates == [True]

    coordinator._device.connected = False
    namespace["time"].time = lambda: 1790367333.0
    connect(coordinator)
    assert coordinator.last_connected_at == 1790367222.0
    assert listener_updates == [True]


def test_la_t01_dp71_history_compares_replies_without_exposing_verification():
    namespace = {
        "PRODUCT_8GZA4O8A": "8gza4o8a",
        "PRODUCT_B3AOULUH": "b3aouluh",
        "time": SimpleNamespace(time=lambda: 1793733266.0),
    }
    handle = _method("devices.py", "TuyaBLECoordinator", "_async_handle_update", namespace)
    device = SimpleNamespace(
        product_id="8gza4o8a",
        connected=True,
        _cfm_dp71_last_result=None,
        _cfm_dp71_response_history=[],
        _cfm_dp71_previous_verification=None,
        _cfm_received_dp_events=[],
    )
    coordinator = SimpleNamespace(
        _device=device,
        _disconnected=False,
        async_set_updated_data=lambda _: None,
    )

    def reply(verification):
        value = b"\xff\xff\x00\x05" + verification + b"\x01\x6a\xe9\x8e\x7e\x00\x00"
        assert len(value) == 19
        return SimpleNamespace(
            id=71, value=value, type=SimpleNamespace(name="DT_RAW"),
            timestamp=1793733266, flags=0,
        )

    for verification in [b"secret01", b"secret01", b"secret02"]:
        handle(coordinator, [reply(verification)])

    assert [
        item["same_verification_as_previous"]
        for item in device._cfm_dp71_response_history
    ] == [None, True, False]
    assert [item["result_code"] for item in device._cfm_dp71_response_history] == [0, 0, 0]
    assert all(event["datapoints"][0]["value"] == "<redacted>" for event in device._cfm_received_dp_events)
    assert "secret01" not in repr(device._cfm_dp71_response_history)
    assert "secret02" not in repr(device._cfm_dp71_response_history)

    for _ in range(25):
        handle(coordinator, [reply(b"secret02")])
    assert len(device._cfm_dp71_response_history) == 20

    invalidations = []

    def invalidate():
        invalidations.append(True)
        device._cfm_dp71_local_command = None

    device._cfm_dp71_session = SimpleNamespace(invalidate=invalidate)
    device._cfm_dp71_local_command = b"private-seed"
    handle(coordinator, [SimpleNamespace(
        id=70, value=b"new-pairing", type=SimpleNamespace(name="DT_RAW"),
        timestamp=1793733266, flags=0,
    )])
    assert device._cfm_dp71_local_command is None
    assert device._cfm_received_dp_events[-1]["datapoints"][0]["value"] == "<redacted>"

    device._cfm_dp71_local_command = b"private-seed"
    failed = reply(b"secret02")
    failed.value = failed.value[:-1] + b"\x03"
    handle(coordinator, [failed])
    assert device._cfm_dp71_local_command is None
    assert invalidations == [True, True]


def test_update_interval_reports_zero_outside_periodic_mode():
    tree = ast.parse((ROOT / "sensor.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TuyaBLESyncIntervalSensor")
    cls.bases = []
    namespace = {
        "EntityCategory": SimpleNamespace(DIAGNOSTIC="diagnostic"),
        "get_device_info": lambda device: {},
        "connection_mode": lambda entry: entry.options["connection_mode"],
        "sync_interval_minutes": lambda entry: entry.options["sync_interval_minutes"],
        "CONNECTION_MODE_PERIODIC_SYNC": "periodic_sync",
    }
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "sensor.py", "exec"), namespace)
    sensor = namespace["TuyaBLESyncIntervalSensor"]
    device = SimpleNamespace(device_id="test-lock")
    for mode, expected in [("periodic_sync", 60), ("power_save", 0), ("keep_alive", 0), ("on_demand", 0)]:
        entry = SimpleNamespace(options={"connection_mode": mode, "sync_interval_minutes": 60})
        assert sensor(entry, device)._attr_native_value == expected


def test_manual_refresh_button_connects_once_and_keeps_response_window_open():
    tree = ast.parse((ROOT / "button.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TuyaBLERefreshButton")
    cls.bases = [ast.Name(id="FakeBase", ctx=ast.Load())]
    calls = []

    class FakeBase:
        def __init__(self, hass, coordinator, device, product, description, domain):
            self._device = device
            self.description = description

    namespace = {
        "FakeBase": FakeBase,
        "ButtonEntityDescription": lambda **kw: SimpleNamespace(**kw),
    }
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(compile(module, "button.py", "exec", flags=__import__("__future__").annotations.compiler_flag), namespace)

    async def reconnect_and_update():
        calls.append("refresh")

    device = SimpleNamespace(
        reconnect_and_update=reconnect_and_update,
        _lock_power_saver_touch=lambda seconds: calls.append(seconds),
    )
    data = SimpleNamespace(device=device, coordinator=SimpleNamespace(connected=False), product=None)
    button = namespace["TuyaBLERefreshButton"](None, data)
    assert button.available is True
    assert button.description.name == "Actualizar cerradura"
    asyncio.run(button.async_press())
    assert calls == ["refresh", 5.0]
