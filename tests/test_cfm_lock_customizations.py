from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "tuya_ble"
CFM_LOCKS = ("8gza4o8a", "b3aouluh")


def _text(name: str) -> str:
    return (INTEGRATION / name).read_text(encoding="utf-8")


def test_cfm_version_and_tested_power_saving() -> None:
    """CFM builds stay on 0.12.1 and use the hardware-tested lock wrapper."""
    manifest = json.loads(_text("manifest.json"))
    init = _text("__init__.py")
    config_flow = _text("config_flow.py")
    power_saver = _text("lock_power_saver.py")

    assert manifest["version"].startswith("0.12.1-cfm.")
    assert "enable_lock_power_saver(device, idle_disconnect_delay)" in init
    assert "_uses_cfm_lock_power_saver(entry)" in init
    assert "return True" in init  # internal device mode stays persistent for wrapper
    assert 'defaults.get(CONF_CATEGORY) in {"ms", "jtmspro"}' in config_flow
    assert 'LOCK_POWER_SAVER_CATEGORIES = {"ms", "jtmspro"}' in power_saver
    assert "original_ensure_connected = device._ensure_connected" in power_saver
    assert "original_fire_disconnected_callbacks" in power_saver


def test_cfm_jtmspro_products_are_registered() -> None:
    """Both locally supported jtmspro locks remain in the product database."""
    devices = _text("devices.py")

    for product_id in CFM_LOCKS:
        assert product_id in devices


def test_cfm_jtmspro_datapoint_mappings_are_preserved() -> None:
    """Keep the hardware-tested CFM datapoints when rebasing on upstream."""
    expectations = {
        "button.py": ("dp_id=6", 'key="bluetooth_unlock"'),
        "select.py": ("dp_id=31", 'key="beep_volume"'),
        "binary_sensor.py": ("dp_id=47", 'key="lock_motor_state"'),
        "sensor.py": ("TuyaBLEAlarmLockStateMapping(dp_id=21)", "dp_id=9"),
    }

    for filename, markers in expectations.items():
        text = _text(filename)
        for product_id in CFM_LOCKS:
            assert product_id in text
        for marker in markers:
            assert marker in text


def test_upstream_entity_platform_fix_is_present() -> None:
    """Entities must generate IDs from their actual HA platform, not sensor.*."""
    devices = _text("devices.py")
    button = _text("button.py")
    select = _text("select.py")
    binary_sensor = _text("binary_sensor.py")

    assert 'f"{self.platform}.{{}}"' in devices
    assert "platform = Platform.BUTTON" in button
    assert "platform = Platform.SELECT" in select
    assert "platform = Platform.BINARY_SENSOR" in binary_sensor
