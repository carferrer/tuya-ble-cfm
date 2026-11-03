"""Exercise the installed integration against real Home Assistant imports."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import CONF_ADDRESS, CONF_DEVICE_ID, Platform
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

from custom_components.tuya_ble import _platforms_for_device, async_setup_entry
from custom_components.tuya_ble import binary_sensor, button, config_flow, select, sensor
from custom_components.tuya_ble.devices import get_product_info_by_ids
from custom_components.tuya_ble.cloud import HASSTuyaBLEDeviceManager
from custom_components.tuya_ble.tuya_ble import TuyaBLEDataPointType
from custom_components.tuya_ble.const import (
    CONF_CATEGORY,
    CONF_DEVICE_NAME,
    CONF_LOCAL_KEY,
    CONF_PRODUCT_MODEL,
    CONF_PRODUCT_NAME,
    CONF_PRODUCT_ID,
    CONF_UUID,
)


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
        ("8gza4o8a", False, True),
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


def test_la_t01_exposes_refresh_and_dp71_without_dp6():
    """Keep the working DP71 path and remove the failed DP6 test."""
    device = SimpleNamespace(category="jtmspro", product_id="8gza4o8a")

    assert get_product_info_by_ids(device.category, device.product_id) is not None
    assert _platforms_for_device(device) == [
        Platform.BUTTON,
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SELECT,
        Platform.EVENT,
        Platform.SWITCH,
    ]
    assert button.get_mapping_by_device(device) == []
    volume_mapping = select.get_mapping_by_device(device)
    assert {item.dp_id for item in volume_mapping} == {31}
    assert volume_mapping[0].description.key == "beep_volume"
    assert volume_mapping[0].description.translation_key == "beep_volume_experimental"
    assert {item.dp_id for item in sensor.get_mapping_by_device(device)} == {8, 21}
    assert {item.dp_id for item in binary_sensor.get_mapping_by_device(device)} == {33, 47}


def test_la_t01_volume_connects_before_ble_write():
    """Only the LA-T01 volume path wakes a sleeping lock before writing DP31."""
    datapoint = SimpleNamespace(type=TuyaBLEDataPointType.DT_ENUM)

    def get_or_create(*args):
        assert device.connected
        return datapoint

    device = SimpleNamespace(
        category="jtmspro",
        product_id="8gza4o8a",
        connected=False,
        datapoints=SimpleNamespace(get_or_create=MagicMock(side_effect=get_or_create)),
        update=AsyncMock(),
    )

    async def reconnect():
        device.connected = True

    device.reconnect = AsyncMock(side_effect=reconnect)
    device._lock_power_saver_touch = MagicMock()
    entity = SimpleNamespace(
        _device=device,
        _mapping=select.get_mapping_by_device(device)[0],
        _attr_options=["mute", "low", "normal", "high"],
        _command_lock=asyncio.Lock(),
        _confirmation=None,
        _desired_value=None,
        _confirmed_option=None,
        async_write_ha_state=MagicMock(),
    )

    async def send(value):
        select.TuyaBLELASelect._handle_volume_updates(
            entity,
            [SimpleNamespace(id=31, type=TuyaBLEDataPointType.DT_ENUM, value=value)],
        )

    datapoint.set_value = AsyncMock(side_effect=send)
    with patch("custom_components.tuya_ble.select.asyncio.sleep", new_callable=AsyncMock) as settle:
        asyncio.run(select.TuyaBLELASelect.async_select_option(entity, "normal"))

    device.reconnect.assert_awaited_once_with()
    settle.assert_awaited_once_with(0.5)
    device.datapoints.get_or_create.assert_called_once_with(31, TuyaBLEDataPointType.DT_ENUM, 2)
    datapoint.set_value.assert_awaited_once_with(2)
    device.update.assert_not_awaited()
    entity.async_write_ha_state.assert_called_once_with()
    assert entity._confirmed_option == "normal"
    device._lock_power_saver_touch.assert_called_once_with(10.0)


def test_la_t01_volume_never_writes_without_ble_connection():
    device = SimpleNamespace(
        connected=False,
        reconnect=AsyncMock(),
        datapoints=SimpleNamespace(get_or_create=MagicMock()),
    )
    entity = SimpleNamespace(
        _device=device,
        _attr_options=["mute", "low", "normal", "high"],
        _command_lock=asyncio.Lock(),
    )

    with pytest.raises(HomeAssistantError, match="no estableció una conexión BLE"):
        asyncio.run(select.TuyaBLELASelect.async_select_option(entity, "high"))

    device.datapoints.get_or_create.assert_not_called()


def test_la_t01_volume_does_not_show_unconfirmed_write():
    datapoint = SimpleNamespace(type=TuyaBLEDataPointType.DT_ENUM, set_value=AsyncMock())
    device = SimpleNamespace(
        connected=True,
        datapoints=SimpleNamespace(get_or_create=MagicMock(return_value=datapoint)),
        update=AsyncMock(),
    )
    entity = SimpleNamespace(
        _device=device,
        _mapping=SimpleNamespace(dp_id=31),
        _attr_options=["mute", "low", "normal", "high"],
        _command_lock=asyncio.Lock(),
        _confirmation=None,
        _desired_value=None,
        _confirmed_option=None,
    )
    with (
        patch("custom_components.tuya_ble.select.VOLUME_CONFIRM_TIMEOUT_SECONDS", 0.001),
        pytest.raises(HomeAssistantError, match="no confirmó DP31"),
    ):
        asyncio.run(select.TuyaBLELASelect.async_select_option(entity, "mute"))

    datapoint.set_value.assert_awaited_once_with(0)
    device.update.assert_awaited_once_with()
    assert entity._confirmed_option is None


def test_la_t01_volume_restores_last_ble_report_not_send_cache():
    """The observed low report wins over an optimistic mute cache value."""
    device = SimpleNamespace(
        datapoints={31: SimpleNamespace(value=0)},
        _cfm_received_dp_events=[
            {"datapoints": [{"id": 31, "type": "DT_ENUM", "value": 1}]},
            {"datapoints": [{"id": 47, "type": "DT_BOOL", "value": False}]},
        ],
    )
    entity = SimpleNamespace(
        _device=device,
        _mapping=SimpleNamespace(dp_id=31),
        _attr_options=["mute", "low", "normal", "high"],
        _confirmed_option=None,
        async_write_ha_state=MagicMock(),
    )
    select.TuyaBLELASelect._restore_last_volume_report(entity)
    assert entity._confirmed_option == "low"
    entity.async_write_ha_state.assert_called_once_with()


def test_existing_lock_credentials_load_without_cloud_and_missing_credentials_do_not_login():
    """An existing entry must never contact Tuya during runtime setup."""
    options = {
        CONF_UUID: "configured-uuid",
        CONF_LOCAL_KEY: "configured-local-key",
        CONF_DEVICE_ID: "lock-1",
        CONF_CATEGORY: "jtmspro",
        CONF_PRODUCT_ID: "b3aouluh",
        CONF_DEVICE_NAME: "Cerradura",
        CONF_PRODUCT_MODEL: "",
        CONF_PRODUCT_NAME: "Smart Lock",
    }
    manager = HASSTuyaBLEDeviceManager(
        MagicMock(), options, allow_cloud_credentials=False
    )
    manager.login = AsyncMock(side_effect=AssertionError("Unexpected Tuya Cloud login"))
    credentials = asyncio.run(manager.get_device_credentials("AA:BB:CC:DD:EE:FF"))
    assert credentials.device_id == "lock-1"
    manager.login.assert_not_awaited()

    manager.data.pop(CONF_PRODUCT_MODEL)
    assert asyncio.run(manager.get_device_credentials("AA:BB:CC:DD:EE:FF")) is not None
    manager.login.assert_not_awaited()

    manager.data.pop(CONF_LOCAL_KEY)
    assert asyncio.run(manager.get_device_credentials("AA:BB:CC:DD:EE:FF")) is None
    manager.login.assert_not_awaited()


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
    with patch.object(config_flow.pycountry.countries, "get", side_effect=country_lookup) as lookup:
        result = asyncio.run(config_flow._show_login_form(flow, {}, {}, {}))
    lookup.assert_called_once_with(alpha_2="ES")
    assert result["step_id"] == "login"
    assert result["data_schema"].schema
