"""DLOCK FD50 negotiation and isolation from the four proven lock families."""

import asyncio
from struct import unpack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from Crypto.Cipher import AES
from bleak_retry_connector import BleakError

from custom_components.tuya_ble.tuya_ble import fd50
from custom_components.tuya_ble.tuya_ble.const import (
    CHARACTERISTIC_NOTIFY, CHARACTERISTIC_WRITE, TuyaBLECode,
)
from custom_components.tuya_ble.tuya_ble.tuya_ble import TuyaBLEDevice

TRANSPORT = "custom_components.tuya_ble.tuya_ble.tuya_ble"


def device_for(product="99gv5nmz"):
    device = TuyaBLEDevice(None, SimpleNamespace(address="DC:23:52:F5:7D:56"))
    device._device_info = SimpleNamespace(
        product_id=product, uuid="test-uuid", device_id="test-device", category="jtmspro"
    )
    device._local_key = b"123456"
    device._login_key = bytes(16)
    return device


def client_for(size=244):
    return SimpleNamespace(
        is_connected=True,
        services=SimpleNamespace(get_characteristic=MagicMock(
            return_value=SimpleNamespace(max_write_without_response_size=size)
        )),
        start_notify=AsyncMock(),
        stop_notify=AsyncMock(),
        write_gatt_char=AsyncMock(),
        disconnect=AsyncMock(),
    )


def handshake(device):
    async def send(*args):
        # fd50.send_packet receives device before the protocol arguments.
        code = args[1]
        if code == TuyaBLECode.FUN_SENDER_DEVICE_INFO:
            device._session_key = bytes(16)
            device._is_bound = True
            device._protocol_version = 4
        elif code == TuyaBLECode.FUN_SENDER_PAIR:
            device._is_paired = True
        return True
    return send


def test_fd50_handshake_negotiates_channels_size_and_session():
    async def run():
        device = device_for()
        client = client_for()
        connected = MagicMock()
        device.register_connected_callback(connected)
        with (
            patch.object(fd50, "establish_connection", AsyncMock(return_value=client)) as connect,
            patch.object(fd50, "send_packet", AsyncMock(side_effect=handshake(device))) as send,
        ):
            await device._ensure_connected()
            await device._ensure_connected()
        assert device.connected
        assert device._fd50_stage == "ready"
        connect.assert_awaited_once()
        assert connect.call_args.kwargs["max_attempts"] == 1
        assert connect.call_args.kwargs["use_services_cache"] is False
        client.start_notify.assert_awaited_once_with(fd50.NOTIFY_UUID, device._notification_handler)
        assert send.await_args_list[0].args[1:3] == (
            TuyaBLECode.FUN_SENDER_DEVICE_INFO, b"\x00\xf4"
        )
        assert send.await_args_list[1].args[1] == TuyaBLECode.FUN_SENDER_PAIR
        connected.assert_called_once()
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["connect", "services", "notify", "device_info", "pair"])
def test_failed_fd50_stage_cleans_up_and_does_not_loop(stage):
    async def run():
        device = device_for()
        client = client_for()
        connect = AsyncMock(return_value=client)
        send = AsyncMock(side_effect=handshake(device))
        if stage == "connect":
            connect.side_effect = BleakError("failed")
        elif stage == "services":
            client.services.get_characteristic.return_value = None
        elif stage == "notify":
            client.start_notify.side_effect = BleakError("failed")
        else:
            async def fail_at_stage(*args):
                code = args[1]
                if (stage == "device_info" or code == TuyaBLECode.FUN_SENDER_PAIR):
                    raise TimeoutError()
                return await handshake(device)(*args)
            send.side_effect = fail_at_stage
        with patch.object(fd50, "establish_connection", connect), patch.object(fd50, "send_packet", send):
            with pytest.raises(BleakError, match=stage):
                await device._ensure_connected()
            with pytest.raises(BleakError, match="wait 10 seconds"):
                await device._ensure_connected()
        connect.assert_awaited_once()
        assert not device.connected
        assert device._client is None
        assert device._fd50_stage == "failed"
        if stage != "connect":
            client.disconnect.assert_awaited_once()
    asyncio.run(run())


def test_fd50_cancel_releases_client_and_connection_lock():
    async def run():
        device = device_for()
        client = client_for()
        with (
            patch.object(fd50, "establish_connection", AsyncMock(return_value=client)),
            patch.object(fd50, "send_packet", AsyncMock(side_effect=asyncio.CancelledError)),
        ):
            with pytest.raises(asyncio.CancelledError):
                await device._ensure_connected()
        client.disconnect.assert_awaited_once()
        assert device._client is None
        assert not device._connect_lock.locked()
        assert device._fd50_stage == "cancelled"
    asyncio.run(run())


@pytest.mark.parametrize("product", ["b3aouluh", "okkyfgfs", "7a4xvbtt", "8gza4o8a"])
def test_legacy_products_keep_original_handshake_and_characteristics(product):
    async def run():
        device = device_for(product)
        client = client_for()
        async def send(code, *args):
            if code == TuyaBLECode.FUN_SENDER_PAIR:
                device._is_paired = True
            return True
        with (
            patch(f"{TRANSPORT}.establish_connection", AsyncMock(return_value=client)) as connect,
            patch.object(fd50, "ensure_connected", AsyncMock(side_effect=AssertionError("FD50 used"))),
            patch.object(device, "_send_packet_while_connected", AsyncMock(side_effect=send)) as sender,
        ):
            await device._ensure_connected()
        assert connect.call_args.kwargs["use_services_cache"] is True
        client.start_notify.assert_awaited_once_with(CHARACTERISTIC_NOTIFY, device._notification_handler)
        assert sender.await_args_list[0].args[:2] == (TuyaBLECode.FUN_SENDER_DEVICE_INFO, b"")
        await device._int_send_packets_locked([b"legacy"])
        client.write_gatt_char.assert_awaited_once_with(CHARACTERISTIC_WRITE, b"legacy", False)
        await device._execute_disconnect()
        client.stop_notify.assert_awaited_once_with(CHARACTERISTIC_NOTIFY)
    asyncio.run(run())


@pytest.mark.parametrize("product,size", [
    ("99gv5nmz", 244), ("99gv5nmz", 20),
    ("b3aouluh", 20), ("okkyfgfs", 20), ("7a4xvbtt", 20), ("8gza4o8a", 20),
])
def test_packet_capacity_is_per_product_and_payload_stays_valid(product, size):
    device = device_for(product)
    # A stray FD50 attribute must not change legacy packet fragmentation.
    device._fd50_write_size = size if product == "99gv5nmz" else 244
    data = b"\x00\xf4"
    packets = device._build_packets(1, TuyaBLECode.FUN_SENDER_DEVICE_INFO, data)
    assert all(len(packet) <= size for packet in packets)
    assert len(packets) == (1 if size == 244 else 2)
    encrypted = b""
    for index, packet in enumerate(packets):
        assert packet[0] == index
        encrypted += packet[3:] if index == 0 else packet[1:]
    assert packets[0][2] == 0x20
    raw = AES.new(device._login_key, AES.MODE_CBC, encrypted[1:17]).decrypt(encrypted[17:])
    assert unpack(">IIHH", raw[:12]) == (1, 0, 0, 2)
    assert raw[12:14] == data
    assert int.from_bytes(raw[14:16], "big") == device._calc_crc16(raw[:14])


def test_fd50_timeout_removes_pending_response_and_uses_new_write_channel():
    async def run():
        device = device_for()
        client = device._client = client_for()
        device._session_key = bytes(16)
        with patch.object(fd50, "RESPONSE_TIMEOUT", 0.001):
            with pytest.raises(TimeoutError):
                await device._send_packet_while_connected(
                    TuyaBLECode.FUN_SENDER_DEVICE_STATUS, b"", 0, True
                )
        assert not device._input_expected_responses
        assert not device._operation_lock.locked()
        assert all(call.args[0] == fd50.WRITE_UUID for call in client.write_gatt_char.await_args_list)
        await device._execute_disconnect()
        client.stop_notify.assert_awaited_once_with(fd50.NOTIFY_UUID)
        client.disconnect.assert_awaited_once()
    asyncio.run(run())
