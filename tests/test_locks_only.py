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

    # Representative products from the old generic integration must not return.
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

    for platform in ("BUTTON", "SENSOR", "BINARY_SENSOR", "SELECT"):
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


def test_legacy_transport_is_preserved() -> None:
    transport = _text("tuya_ble/tuya_ble.py")
    transport_const = _text("tuya_ble/const.py")

    # Markers from the hardware-tested transport. Do not silently replace this
    # with the newer 0.12.x transport before a dedicated hardware validation.
    assert "attempts_count = 100" in transport
    assert "_parse_datapoints_v3" in transport
    assert "FUN_RECEIVE_SIGN_TIME_DP" in transport
    assert 'SERVICE_UUID = "0000fd50-0000-1000-8000-00805f9b34fb"' in transport_const
    assert "SERVICE_CHARACTERISTICS" not in transport_const


def test_passive_advertisement_capture_and_activity_refresh() -> None:
    init = _text("__init__.py")
    diagnostics = _text("diagnostics.py")

    assert "_cfm_advertisement_history" in init
    assert "manufacturer_data" in init
    assert "service_data" in init
    assert "device._decode_advertisement_data()" in init
    assert "bluetooth.async_last_service_info" in init
    assert "CFM_ACTIVITY_QUIET_INTERVAL = 2.5" in init
    assert "CFM_ACTIVITY_FAST_INTERVAL = 0.9" in init
    assert "CFM_ACTIVITY_REQUIRED_FAST_INTERVALS = 3" in init
    assert "await device.update()" in init
    assert "activity_triggered" in init
    assert '"advertisement_history"' in diagnostics
    assert '"advertisement_events"' in diagnostics
