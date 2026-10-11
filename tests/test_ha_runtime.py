"""Exercise the installed integration against real Home Assistant imports."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.tuya_ble import _platforms_for_device, async_setup_entry
from custom_components.tuya_ble import binary_sensor, button, config_flow, select, sensor
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
        ("7a4xvbtt", True, True),
        ("unsupported", False, False),
    ),
)
def test_supported_lock_platforms(product_id, has_lock, has_switch):
    """Keep the actual HA platform routing for each lock family."""
    platforms = _platforms_for_device(SimpleNamespace(product_id=product_id))
    assert Platform.EVENT in platforms
    assert (Platform.LOCK in platforms) is has_lock
    assert (Platform.SWITCH in platforms) is has_switch


def test_7a4xvbtt_exposes_reports_and_tested_dp6_controls():
    """Keep only the tested DP6 and experimental DP33 write controls."""
    device = SimpleNamespace(category="ms", product_id="7a4xvbtt")

    assert get_product_info_by_ids(device.category, device.product_id) is not None
    assert _platforms_for_device(device) == [
        Platform.BUTTON,
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.EVENT,
        Platform.LOCK,
        Platform.SWITCH,
    ]
    assert {item.dp_id for item in button.get_mapping_by_device(device)} == {6}
    assert select.get_mapping_by_device(device) == []
    assert {item.dp_id for item in sensor.get_mapping_by_device(device)} == {8, 21}
    assert {item.dp_id for item in binary_sensor.get_mapping_by_device(device)} == {33, 47}


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



def test_country_lookup_does_not_block_home_assistant_loop():
    """The login form must read pycountry data in an executor thread."""
    def country_lookup(*, alpha_2):
        assert alpha_2 == "ES"
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return SimpleNamespace(name="Spain")

    class FakeHass:
        config = SimpleNamespace(country="ES")

        async def async_add_executor_job(self, fn):
            return await asyncio.to_thread(fn)

    flow = SimpleNamespace(
        hass=FakeHass(), async_show_form=lambda **kwargs: kwargs
    )
    with patch.object(config_flow.pycountry.countries, "get", side_effect=country_lookup):
        result = asyncio.run(config_flow._show_login_form(flow, {}, {}, {}))
    assert result["step_id"] == "login"
    assert result["data_schema"].schema
