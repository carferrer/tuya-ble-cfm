from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "tuya_ble"


def _text(name: str) -> str:
    return (INTEGRATION / name).read_text(encoding="utf-8")


def test_connection_policy_options_are_exposed_per_entry() -> None:
    config_flow = _text("config_flow.py")
    policy = _text("connection_policy.py")

    assert 'CONF_CONNECTION_MODE = "connection_mode"' in policy
    assert 'CONF_SYNC_INTERVAL = "sync_interval_minutes"' in policy
    assert 'CONNECTION_MODE_POWER_SAVE = "power_save"' in policy
    assert 'CONNECTION_MODE_PERIODIC_SYNC = "periodic_sync"' in policy
    assert 'CONNECTION_MODE_KEEP_ALIVE = "keep_alive"' in policy
    assert "DEFAULT_SYNC_INTERVAL = 5" in policy
    for interval in (1, 2, 5, 10, 15, 30):
        assert f"{interval}:" in policy

    assert "include_connection_options=True" in config_flow
    assert "CONF_CONNECTION_MODE" in config_flow
    assert "CONF_SYNC_INTERVAL" in config_flow
    assert "options[CONF_CONNECTION_MODE]" in config_flow
    assert "options[CONF_SYNC_INTERVAL]" in config_flow


def test_periodic_sync_and_keep_alive_use_existing_ble_transport() -> None:
    policy = _text("connection_policy.py")
    event = _text("event.py")
    power_saver = _text("lock_power_saver.py")
    diagnostics = _text("diagnostics.py")

    assert "setup_connection_policy(hass, entry, data.device)" in event
    assert "async_track_time_interval" in policy
    assert "await device.reconnect()" in policy
    assert policy.count("await device.update()") >= 3
    assert "if device.connected:" in policy
    assert "touch(5.0)" in policy
    assert "KEEP_ALIVE_WATCHDOG_SECONDS = 30" in policy
    assert "_cfm_keep_alive_attempt_count" in policy
    assert "_cfm_keep_alive_success_count" in policy

    assert 'KEEP_ALIVE_MODE = "keep_alive"' in power_saver
    assert "self._lock_connection_mode == KEEP_ALIVE_MODE" in power_saver
    assert "await original_reconnect()" in power_saver

    assert '"connection_policy"' in diagnostics
    assert '"sync_interval_minutes"' in diagnostics
    assert '"periodic_sync_attempt_count"' in diagnostics
    assert '"periodic_sync_success_count"' in diagnostics
    assert '"periodic_sync_last_error"' in diagnostics


def test_periodic_sync_disables_advertising_activity_reconnects() -> None:
    policy = _text("connection_policy.py")

    assert "activity_detection_enabled = mode == CONNECTION_MODE_POWER_SAVE" in policy
    assert "device._cfm_activity_detection_enabled = activity_detection_enabled" in policy
    assert "device._cfm_activity_armed = False" in policy
    assert "device._cfm_activity_fast_streak = 0" in policy
    assert "device._cfm_activity_update_in_progress = not activity_detection_enabled" in policy

    # Periodic mode must not be skipped because the activity detector is
    # intentionally suppressed by the policy marker above.
    assert 'getattr(device, "_cfm_activity_update_in_progress", False)' not in policy
