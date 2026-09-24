from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "tuya_ble"


def _text(name: str) -> str:
    return (INTEGRATION / name).read_text(encoding="utf-8")


def test_only_supported_lock_products_are_registered() -> None:
    devices = _text("devices.py")

    assert '"okkyfgfs"' in devices
    assert '"b3aouluh"' in devices

    for product_id in (
        "8gza4o8a",
        "ludzroix",
        "isk2p555",
        "3yqdo5yt",
        "xhf790if",
        "59s19z5m",
        "ojzlzzsw",
        "cdlandip",
    ):
        assert product_id not in devices


def test_only_lock_platforms_are_loaded() -> None:
    init = _text("__init__.py")

    for platform in ("BUTTON", "SENSOR", "BINARY_SENSOR", "SELECT", "EVENT"):
        assert f"Platform.{platform}" in init

    for platform in ("CLIMATE", "NUMBER", "LIGHT", "SWITCH", "TEXT", "COVER"):
        assert f"Platform.{platform}" not in init

    for filename in (
        "climate.py",
        "number.py",
        "light.py",
        "switch.py",
        "text.py",
        "cover.py",
    ):
        assert not (INTEGRATION / filename).exists()


def test_lock_datapoints_match_hardware_tested_configuration() -> None:
    button = _text("button.py")
    select = _text("select.py")
    binary_sensor = _text("binary_sensor.py")
    sensor = _text("sensor.py")

    assert "dp_id=6" in button
    assert 'key="bluetooth_unlock"' in button
    assert "dp_id=31" in select
    assert 'key="beep_volume"' in select
    assert "dp_id=47" in binary_sensor
    assert 'key="lock_motor_state"' in binary_sensor
    assert "dp_id=21" in sensor
    assert 'key="alarm_lock"' in sensor
    assert "TuyaBLEBatteryMapping(dp_id=8)" in sensor
    assert "dp_id=9" in sensor
    assert 'key="battery_state"' in sensor


def test_proven_power_saver_is_enabled() -> None:
    init = _text("__init__.py")
    power_saver = _text("lock_power_saver.py")

    assert "enable_lock_power_saver(device)" in init
    assert 'LOCK_POWER_SAVER_CATEGORIES = {"ms", "jtmspro"}' in power_saver
    assert "DEFAULT_LOCK_IDLE_DISCONNECT_DELAY = 30" in power_saver
    assert "def _touch(self: Any, delay: float | None = None)" in power_saver


def test_legacy_transport_is_preserved() -> None:
    transport = _text("tuya_ble/tuya_ble.py")
    transport_const = _text("tuya_ble/const.py")

    assert "attempts_count = 100" in transport
    assert "_parse_datapoints_v3" in transport
    assert "FUN_RECEIVE_SIGN_TIME_DP" in transport
    assert 'SERVICE_UUID = "0000fd50-0000-1000-8000-00805f9b34fb"' in transport_const
    assert "SERVICE_CHARACTERISTICS" not in transport_const


def test_product_specific_activity_detection() -> None:
    init = _text("__init__.py")
    diagnostics = _text("diagnostics.py")

    assert 'PRODUCT_B3AOULUH = "b3aouluh"' in init
    assert 'PRODUCT_OKKYFGFS = "okkyfgfs"' in init

    assert "B3_ACTIVITY_QUIET_INTERVAL = 4.0" in init
    assert "B3_ACTIVITY_FAST_INTERVAL = 0.7" in init
    assert "B3_ACTIVITY_REQUIRED_FAST_INTERVALS = 3" in init
    assert "B3_ACTIVITY_IDLE_DISCONNECT_DELAY = 3.0" in init
    assert "B3_ACTIVITY_COOLDOWN = 30.0" in init
    assert "_b3_activity_cooldown_remaining" in init
    assert "b3_cooldown_remaining > 0" in init
    assert '"cooldown_remaining_ms"' in init
    assert '"burst"' in init
    assert "B3_ACTIVITY_SECOND_BURST_WINDOW" not in init
    assert '"double_burst"' not in init

    assert "OKKY_ACTIVITY_QUIET_INTERVAL = 15.0" in init
    assert "OKKY_ACTIVITY_FAST_INTERVAL = 0.7" in init
    assert "OKKY_ACTIVITY_REQUIRED_FAST_INTERVALS = 1" in init
    assert "OKKY_ACTIVITY_IDLE_DISCONNECT_DELAY = 8.0" in init
    assert '"payload_change"' in init
    assert '"sparse_burst"' in init

    assert "bluetooth.async_last_service_info" in init
    assert "device._decode_advertisement_data()" in init
    assert "CFM_ACTIVITY_GATT_SETTLE_DELAY = 0.1" in init
    assert "await device.reconnect()" in init
    assert init.count("await device.update()") >= 2
    assert "power_saver_touch(idle_delay)" in init

    assert '"observed_at"' in init
    assert '"trigger_reason"' in init
    assert '"gatt_connected"' in init
    assert '"duration_ms"' in init
    assert "_cfm_activity_refresh_history" in init

    assert '"activity_detector"' in diagnostics
    assert '"trigger_count"' in diagnostics
    assert '"refresh_attempt_count"' in diagnostics
    assert '"connect_count"' in diagnostics
    assert '"refresh_count"' in diagnostics
    assert '"last_trigger_reason"' in diagnostics
    assert '"last_refresh_error"' in diagnostics
    assert '"last_refresh_dp47_before"' in diagnostics
    assert '"last_refresh_dp47_after"' in diagnostics
    assert '"refresh_history"' in diagnostics
    assert '"payload_change_count"' in diagnostics
    assert '"gatt_connected"' in diagnostics


def test_received_local_datapoints_are_exposed_for_lock_record_analysis() -> None:
    devices = _text("devices.py")
    diagnostics = _text("diagnostics.py")

    assert "_cfm_received_dp_events" in devices
    assert '"received_at"' in devices
    assert '"datapoints"' in devices
    assert '"flags"' in devices
    assert '"timestamp"' in devices
    assert '"received_dp_events"' in diagnostics


def test_b3_dp69_cached_record_recovery_is_local_and_scoped() -> None:
    devices = _text("devices.py")
    diagnostics = _text("diagnostics.py")

    assert 'PRODUCT_B3AOULUH = "b3aouluh"' in devices
    assert "DP_GET_RECORDS = 69" in devices
    assert "DP_GET_RECORDS_REQUEST_ACTION = 0x01" in devices
    assert 'MOBILE_CENTRAL_ID = b"\\xff\\xff"' in devices
    assert "INITIAL_MOBILE_RANDOM = bytes(8)" in devices
    assert "self._device.product_id == PRODUCT_B3AOULUH" in devices
    assert "len(value) == 3" in devices
    assert "value[2] == DP_GET_RECORDS_REQUEST_ACTION" in devices
    assert "MOBILE_CENTRAL_ID" in devices
    assert "+ peripheral_id" in devices
    assert "+ INITIAL_MOBILE_RANDOM" in devices
    assert "await datapoint.set_value(response)" in devices
    assert "self._dp69_response_client is not client" in devices

    assert '"record_recovery"' in diagnostics
    assert '"dp69_request_count"' in diagnostics
    assert '"dp69_response_attempt_count"' in diagnostics
    assert '"dp69_response_count"' in diagnostics
    assert '"dp69_last_request"' in diagnostics
    assert '"dp69_last_response"' in diagnostics
    assert '"dp69_last_error"' in diagnostics


def test_b3_declared_access_methods_are_exposed_as_persistent_events() -> None:
    access = _text("access.py")
    event = _text("event.py")
    sensor = _text("sensor.py")

    expected_dp_constants = {
        "DP_FINGERPRINT_UNLOCK": 12,
        "DP_PASSWORD_UNLOCK": 13,
        "DP_DYNAMIC_PASSWORD_UNLOCK": 14,
        "DP_CARD_UNLOCK": 15,
        "DP_BLE_UNLOCK": 19,
        "DP_TEMPORARY_PASSWORD_UNLOCK": 55,
        "DP_PHONE_REMOTE_UNLOCK": 62,
        "DP_VOICE_REMOTE_UNLOCK": 63,
    }
    for constant, dp_id in expected_dp_constants.items():
        assert f"{constant} = {dp_id}" in access

    for event_type in (
        "fingerprint_unlock",
        "password_unlock",
        "dynamic_password_unlock",
        "card_unlock",
        "ble_unlock",
        "temporary_password_unlock",
        "phone_remote_unlock",
        "voice_remote_unlock",
    ):
        assert f'"{event_type}"' in access

    assert 'DP_FINGERPRINT_UNLOCK: (EVENT_FINGERPRINT_UNLOCK, "fingerprint")' in access
    assert 'DP_PASSWORD_UNLOCK: (EVENT_PASSWORD_UNLOCK, "password")' in access
    assert "ACCESS_STORE_MAX_KEYS = 200" in access
    assert "ACCESS_RECORD_REPLAY_DELAY = 2.0" in access
    assert "access_record_key" in access
    assert '"method": method' in access
    assert '"recovered"' in access

    # Mechanical/inside/lock records are intentionally not inferred as access methods.
    assert "DP_MECHANICAL" not in access
    assert "DP_INSIDE" not in access
    assert "DP_LOCK_RECORD" not in access

    assert "class TuyaBLEAccessEvent(EventEntity)" in event
    assert "_attr_event_types = ACCESS_EVENT_TYPES" in event
    assert "self._trigger_event" in event
    assert "Store(" in event
    assert "seen_keys" in event
    assert "last_record" in event
    assert "establish a baseline" in event
    assert "register_callback(self._handle_updates)" in event

    assert "class TuyaBLELastAccessSensor(SensorEntity)" in sensor
    assert "SensorDeviceClass.TIMESTAMP" in sensor
    assert '"member_id"' in sensor
    assert '"recovered"' in sensor
    assert "newest_access_record_from_history" in sensor
