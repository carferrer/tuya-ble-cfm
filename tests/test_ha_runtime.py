"""Exercise the installed integration against real Home Assistant imports."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.tuya_ble import _platforms_for_device, async_setup_entry
from custom_components.tuya_ble import binary_sensor, button, select, sensor
from custom_components.tuya_ble.devices import get_product_info_by_ids


@pytest.mark.parametrize(
    "module",
    (
        "binary_sensor",
        "button",
        "config_flow",
        "event",
        "lock",
        "select",
        "sensor",
        "switch",
    ),
)
def test_platform_modules_import_with_home_assistant(module):
    """Catch incompatible HA APIs and missing runtime dependencies."""
    importlib.import_module(f"custom_components.tuya_ble.{module}")


@pytest.mark.parametrize(
    ("product_id", "has_lock", "has_switch"),
    (
        ("b3aouluh", True, True),
        ("okkyfgfs", True, False),
        ("7a4xvbtt", False, False),
        ("unsupported", False, False),
    ),
)
def test_supported_lock_platforms(product_id, has_lock, has_switch):
    """Keep the actual HA platform routing for each lock family."""
    platforms = _platforms_for_device(SimpleNamespace(product_id=product_id))
    assert Platform.EVENT in platforms
    assert (Platform.LOCK in platforms) is has_lock
    assert (Platform.SWITCH in platforms) is has_switch


def test_7a4xvbtt_exposes_reports_and_only_the_experimental_dp6_button():
    """Keep DP33, lock, and unrelated write controls unavailable."""
    device = SimpleNamespace(category="ms", product_id="7a4xvbtt")

    assert get_product_info_by_ids(device.category, device.product_id) is not None
    assert _platforms_for_device(device) == [
        Platform.BUTTON,
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.EVENT,
    ]
    assert {item.dp_id for item in button.get_mapping_by_device(device)} == {6}
    assert select.get_mapping_by_device(device) == []
    assert {item.dp_id for item in sensor.get_mapping_by_device(device)} == {8, 21}
    assert {item.dp_id for item in binary_sensor.get_mapping_by_device(device)} == {47}


def test_setup_retries_when_ble_device_is_missing():
    """Exercise the real setup entry point without requiring BLE hardware."""
    address = "AA:BB:CC:DD:EE:FF"
    entry = SimpleNamespace(data={CONF_ADDRESS: address})
    hass = MagicMock()

    with (
        patch(
            "custom_components.tuya_ble.bluetooth.async_ble_device_from_address",
            return_value=None,
        ) as find_cached_device,
        patch(
            "custom_components.tuya_ble.get_device",
            new_callable=AsyncMock,
            return_value=None,
        ) as find_device,
        pytest.raises(ConfigEntryNotReady, match="Could not find Tuya BLE device"),
    ):
        asyncio.run(async_setup_entry(hass, entry))

    find_cached_device.assert_called_once_with(hass, address, True)
    find_device.assert_awaited_once_with(address)

