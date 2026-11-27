"""DLOCK-only V4 writes and controls; no commands leak to other products."""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak_retry_connector import BleakError
from homeassistant.exceptions import HomeAssistantError

from custom_components.tuya_ble import button, select
from custom_components.tuya_ble.dlock_controls import DLOCKUnlockTestButton, DLOCKVolumeSelect
from custom_components.tuya_ble.tuya_ble import fd50, fd50_commands
from custom_components.tuya_ble.tuya_ble.const import TuyaBLECode, TuyaBLEDataPointType as DP
from custom_components.tuya_ble.tuya_ble.exceptions import TuyaBLEDeviceError
from test_fd50_transport import device_for, client_for

CONTROLS = "custom_components.tuya_ble.dlock_controls"


@pytest.mark.parametrize("status", [0, 1])
def test_v4_write_payload_and_ack_result(status):
    async def run():
        device = device_for()
        device._client = client_for()
        device._is_paired = True
        device._protocol_version = 4
        captured = []
        def build(seq, code, data, response_to):
            captured.append((code, data, response_to))
            return [b"encrypted"]
        device._build_packets = MagicMock(side_effect=build)
        async def write(*args, **kwargs):
            seq = next(iter(device._fd50_write_headers))
            data = device._fd50_write_headers[seq] + bytes((status,))
            device._handle_command_or_response(10, seq, TuyaBLECode.FUN_SENDER_DPS_V4, data)
        device._client.write_gatt_char.side_effect = write
        if status:
            with pytest.raises(TuyaBLEDeviceError):
                await fd50_commands.write_dp(device, 31, DP.DT_ENUM, b"\x02")
        else:
            await fd50_commands.write_dp(device, 31, DP.DT_ENUM, b"\x02")
        assert captured == [(TuyaBLECode.FUN_SENDER_DPS_V4,
                             b"\x00\x00\x00\x00\x01\x1f\x04\x00\x01\x02", 0)]
        assert not device._fd50_write_headers
        assert not device._input_expected_responses
        assert device.datapoints[31] is None
        device._client.write_gatt_char.assert_awaited_once_with(
            fd50.WRITE_UUID, b"encrypted", response=False
        )
    asyncio.run(run())


@pytest.mark.parametrize("reply", [b"", b"\x00" * 5, b"\x01" + b"\x00" * 5])
def test_invalid_write_ack_is_not_success(reply):
    async def run():
        device = device_for()
        future = asyncio.get_running_loop().create_future()
        device._fd50_write_headers = {1: b"\x00" * 5}
        device._input_expected_responses = {1: future}
        fd50_commands.handle_write_response(device, 1, reply)
        with pytest.raises(BleakError):
            await future
    asyncio.run(run())


def test_v4_timeout_cleans_pending_headers_without_retry():
    async def run():
        device = device_for()
        device._client = client_for()
        device._is_paired = True
        device._build_packets = MagicMock(return_value=[b"encrypted"])
        with patch.object(fd50, "RESPONSE_TIMEOUT", 0.001):
            with pytest.raises(TimeoutError):
                await fd50_commands.write_dp(device, 31, DP.DT_ENUM, b"\x01")
        assert not device._fd50_write_headers
        assert not device._input_expected_responses
        device._client.write_gatt_char.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("product", ["b3aouluh", "okkyfgfs", "7a4xvbtt", "8gza4o8a"])
def test_controls_refuse_every_legacy_product(product):
    device = SimpleNamespace(product_id=product, reconnect=AsyncMock())
    with pytest.raises(ValueError):
        asyncio.run(fd50_commands.write_dp(device, 31, DP.DT_ENUM, b"\x01"))
    entity = SimpleNamespace(_device=device)
    with pytest.raises(HomeAssistantError):
        asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    with pytest.raises(HomeAssistantError):
        asyncio.run(DLOCKVolumeSelect.async_select_option(entity, "low"))
    device.reconnect.assert_not_awaited()


def test_volume_waits_for_real_report_and_preserves_previous_value_on_timeout():
    async def run():
        device = SimpleNamespace(product_id="99gv5nmz")
        entity = SimpleNamespace(
            _device=device, _attr_options=["mute", "low", "normal", "high"],
            _confirmed_option="low", _confirmation=None, _desired_value=None,
        )
        with (
            patch(f"{CONTROLS}.write_dp", new_callable=AsyncMock) as send,
            patch(f"{CONTROLS}.VOLUME_TIMEOUT", 0.001),
        ):
            with pytest.raises(HomeAssistantError, match="confirmación DP31"):
                await DLOCKVolumeSelect.async_select_option(entity, "high")
        send.assert_awaited_once_with(device, 31, DP.DT_ENUM, b"\x03")
        assert entity._confirmed_option == "low"
        assert entity._confirmation is None
        assert not device._dlock_command_lock.locked()
    asyncio.run(run())


@pytest.mark.parametrize("value,option", list(enumerate(["mute", "low", "normal", "high"])))
def test_volume_confirms_matching_report(value, option):
    async def run():
        device = SimpleNamespace(product_id="99gv5nmz")
        entity = DLOCKVolumeSelect.__new__(DLOCKVolumeSelect)
        entity._device = device
        entity._mapping = SimpleNamespace(dp_id=31)
        entity._attr_options = ["mute", "low", "normal", "high"]
        entity._confirmed_option = None
        entity._last_saved_option = None
        entity._store = SimpleNamespace(async_delay_save=MagicMock())
        entity.async_write_ha_state = MagicMock()
        async def send(*args):
            entity._handle_volume_updates([SimpleNamespace(id=31, type=DP.DT_ENUM, value=value)])
        with patch(f"{CONTROLS}.write_dp", AsyncMock(side_effect=send)):
            await entity.async_select_option(option)
        assert entity.current_option == option
        entity._store.async_delay_save.assert_called_once()
    asyncio.run(run())


def test_unlock_missing_cloud_report_sends_nothing():
    entity = SimpleNamespace(
        _device=SimpleNamespace(product_id="99gv5nmz", device_id="dlock-2"),
        _manager=SimpleNamespace(get_lock_unlock_properties=AsyncMock(return_value=[])),
    )
    with patch(f"{CONTROLS}.write_dp", new_callable=AsyncMock) as send:
        with pytest.raises(HomeAssistantError, match="informe DP71 compatible"):
            asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    send.assert_not_awaited()
    entity._manager.get_lock_unlock_properties.assert_awaited_once_with("dlock-2")


def test_unlock_uses_own_report_and_one_dp71_without_dp6():
    report = b"\x00\x05\xff\xff12345678\x01" + bytes(6)
    properties = [
        {"code": "ble_unlock_check", "value": base64.b64encode(report).decode(), "time": 1},
        {"code": "unlock_ble", "value": 1},
    ]
    device = SimpleNamespace(product_id="99gv5nmz", device_id="dlock-3")
    entity = SimpleNamespace(
        _device=device,
        _manager=SimpleNamespace(get_lock_unlock_properties=AsyncMock(return_value=properties)),
    )
    with patch(f"{CONTROLS}.write_dp", new_callable=AsyncMock) as send:
        asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    send.assert_awaited_once()
    assert send.call_args.args[:3] == (device, 71, DP.DT_RAW)
    assert len(send.call_args.args[3]) == 19
    assert send.call_args.args[3][:12] == b"\xff\xff\x00\x0512345678"


def test_disconnected_write_connects_before_send_and_refuses_failed_connection():
    async def run():
        device = SimpleNamespace(
            product_id="99gv5nmz", connected=False, reconnect=AsyncMock(), _client=object()
        )
        with patch.object(fd50_commands, "send_packet", new_callable=AsyncMock) as send:
            with pytest.raises(BleakError):
                await fd50_commands.write_dp(device, 31, DP.DT_ENUM, b"\x00")
            send.assert_not_awaited()
            async def connect():
                device.connected = True
            device.reconnect.side_effect = connect
            with patch.object(fd50_commands.asyncio, "sleep", new_callable=AsyncMock):
                await fd50_commands.write_dp(device, 31, DP.DT_ENUM, b"\x00")
            send.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("product", ["99gv5nmz", "b3aouluh", "okkyfgfs", "7a4xvbtt", "8gza4o8a"])
def test_entity_setup_adds_new_controls_only_to_dlock(product):
    async def run():
        from custom_components.tuya_ble.const import DOMAIN
        device = MagicMock()
        device.product_id = product
        device.category = "ms" if product in ("okkyfgfs", "7a4xvbtt") else "jtmspro"
        device.device_id = "unit-1"
        data = SimpleNamespace(
            device=device, product=MagicMock(), coordinator=MagicMock(), manager=MagicMock()
        )
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry": data}}
        entry = SimpleNamespace(entry_id="entry")
        buttons, selects = [], []
        with patch.object(button.er, "async_get") as registry:
            registry.return_value.async_get_entity_id.return_value = None
            await button.async_setup_entry(hass, entry, buttons.extend)
            await select.async_setup_entry(hass, entry, selects.extend)
        assert sum(isinstance(e, DLOCKUnlockTestButton) for e in buttons) == (product == "99gv5nmz")
        assert sum(isinstance(e, DLOCKVolumeSelect) for e in selects) == (product == "99gv5nmz")
    asyncio.run(run())
