"""DLOCK-only V4 writes and controls; no commands leak to other products."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak_retry_connector import BleakError
from homeassistant.exceptions import HomeAssistantError

from custom_components.tuya_ble import button, select
from custom_components.tuya_ble.dlock_controls import DLOCKUnlockTestButton, DLOCKVolumeSelect
from custom_components.tuya_ble.dlock_unlock import CONF_DLOCK_SEED, seed_record
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


def local_unlock_entity(tmp_path, unit=1, seed="0005616263646566676801"):
    address = f"AA:BB:CC:DD:EE:{unit:02X}"
    device = SimpleNamespace(
        product_id="99gv5nmz", device_id=f"dlock-{unit}", address=address,
        connected=True, _client=object(), reconnect=AsyncMock(),
    )
    callbacks = []
    unsubscribe = MagicMock(side_effect=lambda: callbacks.clear())
    device.register_callback = MagicMock(side_effect=lambda cb: (callbacks.append(cb), unsubscribe)[1])
    path = tmp_path / "tuya_ble_private" / f"dlock_aabbccddee{unit:02x}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({
        "version": 1, "product_id": "99gv5nmz", "device_id": device.device_id,
        "address": address, "seed": seed,
    }))
    async def executor(fn, *args):
        return fn(*args)
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        async_add_executor_job=AsyncMock(side_effect=executor),
    )
    entity = SimpleNamespace(_device=device, _hass=hass, _config_entry=SimpleNamespace(options={}))
    return entity, path, callbacks, unsubscribe


def reply_to(command, status=0):
    return command[2:4] + command[:2] + command[4:18] + bytes((status,))


@pytest.mark.parametrize("unit", [1, 2, 3])
def test_configured_seed_opens_without_private_file(tmp_path, unit):
    entity, path, callbacks, _ = local_unlock_entity(tmp_path, unit)
    device = entity._device
    record = seed_record("0005616263646566676801", device.product_id, device.device_id, device.address)
    entity._config_entry.options[CONF_DLOCK_SEED] = json.loads(json.dumps(record))
    path.unlink()
    async def send(device, dp, kind, command):
        callbacks[0]([SimpleNamespace(id=71, type=DP.DT_RAW, value=reply_to(command))])
    with patch(f"{CONTROLS}.write_dp", AsyncMock(side_effect=send)) as sender:
        asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    sender.assert_awaited_once()
    assert sender.call_args.args[1] == 71
    entity._hass.async_add_executor_job.assert_not_awaited()
    assert device._dlock_dp71_last_result["status"] == "confirmed"


def test_invalid_configured_seed_does_not_fall_back_to_valid_file(tmp_path):
    entity, path, _, _ = local_unlock_entity(tmp_path)
    record = json.loads(path.read_text())
    record["device_id"] = "another-unit"
    entity._config_entry.options[CONF_DLOCK_SEED] = record
    entity._device.connected = False
    with patch(f"{CONTROLS}.write_dp", new_callable=AsyncMock) as send:
        with pytest.raises(HomeAssistantError):
            asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    send.assert_not_awaited()
    entity._device.reconnect.assert_not_awaited()
    entity._hass.async_add_executor_job.assert_not_awaited()


def test_local_unlock_missing_file_sends_nothing_and_does_not_connect(tmp_path):
    entity, path, _, _ = local_unlock_entity(tmp_path)
    path.unlink()
    entity._device.connected = False
    with patch(f"{CONTROLS}.write_dp", new_callable=AsyncMock) as send:
        with pytest.raises(HomeAssistantError, match="archivo privado"):
            asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    send.assert_not_awaited()
    entity._device.reconnect.assert_not_awaited()


@pytest.mark.parametrize("unit", [1, 2, 3])
def test_local_unlock_own_file_single_dp71_and_confirmed_result(tmp_path, unit):
    entity, _, callbacks, unsubscribe = local_unlock_entity(tmp_path, unit)
    async def send(device, dp, kind, command):
        callbacks[0]([SimpleNamespace(id=71, type=DP.DT_RAW, value=reply_to(command))])
    with (
        patch(f"{CONTROLS}.write_dp", AsyncMock(side_effect=send)) as sender,
        patch(f"{CONTROLS}.time.time", return_value=1800000000),
    ):
        asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    sender.assert_awaited_once()
    args = sender.call_args.args
    assert args[:3] == (entity._device, 71, DP.DT_RAW)
    assert args[3] == b"\xff\xff\x00\x05abcdefgh\x01" + (1800000000).to_bytes(4, "big") + b"\x00\x01"
    assert entity._device._dlock_dp71_last_result == {"status": "confirmed", "response_code": 0}
    entity._hass.async_add_executor_job.assert_awaited_once()
    unsubscribe.assert_called_once()
    assert not entity._device._dlock_command_lock.locked()


@pytest.mark.parametrize("result", [1, 2, 3, 4, 5, 6])
def test_local_unlock_reports_rejection_without_retry(tmp_path, result):
    entity, _, callbacks, unsubscribe = local_unlock_entity(tmp_path)
    async def send(device, dp, kind, command):
        callbacks[0]([SimpleNamespace(id=71, type=DP.DT_RAW, value=reply_to(command, result))])
    with patch(f"{CONTROLS}.write_dp", AsyncMock(side_effect=send)) as sender:
        with pytest.raises(HomeAssistantError, match=f"código {result}"):
            asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    sender.assert_awaited_once()
    unsubscribe.assert_called_once()
    assert entity._device._dlock_dp71_last_result["status"] == "rejected"


@pytest.mark.parametrize("reply_kind", ["none", "stale", "wrong_type", "other_session"])
def test_transport_ack_does_not_confirm_unlock(tmp_path, reply_kind):
    entity, _, callbacks, unsubscribe = local_unlock_entity(tmp_path)
    async def send(device, dp, kind, command):
        reply = reply_to(command)
        if reply_kind == "none":
            return
        if reply_kind == "stale":
            reply = reply[:13] + bytes(4) + reply[17:]
        if reply_kind == "other_session":
            device._client = object()
        callbacks[0]([SimpleNamespace(id=71, type=DP.DT_ENUM if reply_kind == "wrong_type" else DP.DT_RAW, value=reply)])
    with (
        patch(f"{CONTROLS}.write_dp", AsyncMock(side_effect=send)) as sender,
        patch(f"{CONTROLS}.UNLOCK_TIMEOUT", 0.001),
    ):
        with pytest.raises(HomeAssistantError, match="No se confirmó"):
            asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    sender.assert_awaited_once()
    unsubscribe.assert_called_once()
    assert entity._device._dlock_dp71_last_result == {"status": "timeout"}


def test_unlock_connection_precedes_timestamp_and_send(tmp_path):
    entity, _, callbacks, _ = local_unlock_entity(tmp_path)
    device = entity._device
    device.connected = False
    calls = []
    async def connect():
        calls.append("connect")
        device.connected = True
    async def sleep(delay):
        calls.append("settle")
        assert delay == 0.5
    def now():
        calls.append("timestamp")
        return 1800000000
    async def send(device, dp, kind, command):
        calls.append("send")
        callbacks[0]([SimpleNamespace(id=71, type=DP.DT_RAW, value=reply_to(command))])
    device.reconnect.side_effect = connect
    with (
        patch(f"{CONTROLS}.write_dp", AsyncMock(side_effect=send)),
        patch(f"{CONTROLS}.time.time", side_effect=now),
        patch(f"{CONTROLS}.asyncio.sleep", side_effect=sleep),
    ):
        asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    assert calls == ["connect", "settle", "timestamp", "send"]


def test_failed_unlock_connection_sends_nothing(tmp_path):
    entity, _, _, _ = local_unlock_entity(tmp_path)
    entity._device.connected = False
    with patch(f"{CONTROLS}.write_dp", new_callable=AsyncMock) as sender:
        with pytest.raises(HomeAssistantError, match="No se pudo conectar"):
            asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    sender.assert_not_awaited()


@pytest.mark.parametrize("failure", [BleakError("private bytes"), asyncio.CancelledError()])
def test_unlock_failure_cleans_callback_and_does_not_retry(tmp_path, failure):
    entity, _, _, unsubscribe = local_unlock_entity(tmp_path)
    with patch(f"{CONTROLS}.write_dp", AsyncMock(side_effect=failure)) as sender:
        with pytest.raises((HomeAssistantError, asyncio.CancelledError)) as error:
            asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    assert "private bytes" not in str(error.value)
    sender.assert_awaited_once()
    unsubscribe.assert_called_once()
    assert not entity._device._dlock_command_lock.locked()


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
