"""Attempt failure stages, retention, privacy and successful-opening notifications."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.tuya_ble.dlock_attempts import UnlockAttempt, attempt_history
from custom_components.tuya_ble.dlock_controls import DLOCKUnlockTestButton
from custom_components.tuya_ble.tuya_ble import fd50
from custom_components.tuya_ble.tuya_ble.const import TuyaBLECode, TuyaBLEDataPointType as DP
from test_dlock_controls import CONTROLS, local_unlock_entity, reply_to
from test_fd50_transport import client_for, device_for


def test_failed_attempt_retained_when_next_press_succeeds(tmp_path):
    entity, _, callbacks, _ = local_unlock_entity(tmp_path)
    listener = MagicMock()
    entity._device._dlock_open_listeners = [listener]
    async def run():
        with patch(f"{CONTROLS}.write_dp", AsyncMock(side_effect=TimeoutError("PRIVATE"))):
            with pytest.raises(HomeAssistantError):
                await DLOCKUnlockTestButton.async_press(entity)
        async def send(device, dp, kind, command):
            callbacks[0]([SimpleNamespace(id=71, type=DP.DT_RAW, value=reply_to(command))])
        with patch(f"{CONTROLS}.write_dp", AsyncMock(side_effect=send)):
            await DLOCKUnlockTestButton.async_press(entity)
    asyncio.run(run())
    history = attempt_history(entity._device)
    assert [item["status"] for item in history] == ["timeout", "confirmed"]
    assert history[0]["phase"] == "writing"
    assert history[1]["matching_response_received"] is True
    assert history[1]["transport_ack_received"] is True
    assert "PRIVATE" not in json.dumps(history)
    assert "abcdefgh" not in json.dumps(history)
    assert "0005616263646566676801" not in json.dumps(history)
    listener.assert_called_once()
    assert entity._device._dlock_active_unlock_attempt is None


@pytest.mark.parametrize("failure", ["missing_seed", "connect", "response", "cancel"])
def test_failure_stage_and_cleanup(tmp_path, failure):
    entity, _, _, _ = local_unlock_entity(tmp_path)
    if failure == "missing_seed":
        entity._config_entry.options.clear()
    elif failure == "connect":
        entity._device.connected = False
    side_effect = asyncio.CancelledError() if failure == "cancel" else None
    with (
        patch(f"{CONTROLS}.write_dp", AsyncMock(side_effect=side_effect)),
        patch(f"{CONTROLS}.UNLOCK_TIMEOUT", 0.001),
    ):
        with pytest.raises((HomeAssistantError, asyncio.CancelledError)):
            asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    item = attempt_history(entity._device)[0]
    expected = {
        "missing_seed": ("invalid_configuration", "configuration"),
        "connect": ("connection_error", "connecting"),
        "response": ("timeout", "waiting_dp71"),
        "cancel": ("cancelled", "writing"),
    }
    assert (item["status"], item["phase"]) == expected[failure]
    assert entity._device._dlock_active_unlock_attempt is None
    assert not entity._device._dlock_command_lock.locked()


@pytest.mark.parametrize("reply", ["stale", "rejected"])
def test_unsuccessful_reply_never_signals_opening(tmp_path, reply):
    entity, _, callbacks, _ = local_unlock_entity(tmp_path)
    listener = MagicMock()
    entity._device._dlock_open_listeners = [listener]
    async def send(device, dp, kind, command):
        value = reply_to(command, 1) if reply == "rejected" else bytes(19)
        callbacks[0]([SimpleNamespace(id=71, type=DP.DT_RAW, value=value)])
    with (
        patch(f"{CONTROLS}.write_dp", AsyncMock(side_effect=send)),
        patch(f"{CONTROLS}.UNLOCK_TIMEOUT", 0.001),
    ):
        with pytest.raises(HomeAssistantError):
            asyncio.run(DLOCKUnlockTestButton.async_press(entity))
    listener.assert_not_called()
    item = attempt_history(entity._device)[0]
    assert item["status"] == ("rejected" if reply == "rejected" else "timeout")
    assert item["unmatched_reports"] == (reply == "stale")


def test_history_is_bounded_independent_and_snapshot_is_not_mutable():
    a, b = SimpleNamespace(connected=False), SimpleNamespace(connected=True)
    for _ in range(25):
        with UnlockAttempt(a):
            pass
    with UnlockAttempt(b):
        pass
    snapshot = attempt_history(a)
    assert len(snapshot) == 20
    assert snapshot[0]["attempt"] == 6
    assert attempt_history(b)[0]["attempt"] == 1
    snapshot[0]["timeline"].clear()
    assert attempt_history(a)[0]["timeline"]
    assert snapshot[0]["started_at"].endswith("+00:00")


@pytest.mark.parametrize("failure", ["write", "ack", "success"])
def test_actual_transport_distinguishes_packet_write_and_ack(failure):
    async def run():
        device = device_for()
        device._client = client_for()
        device._build_packets = MagicMock(return_value=[b"ciphertext"])
        async def write(*args, **kwargs):
            if failure == "write":
                raise TimeoutError()
            if failure == "success":
                next(iter(device._input_expected_responses.values())).set_result(0)
        device._client.write_gatt_char.side_effect = write
        with patch.object(fd50, "RESPONSE_TIMEOUT", 0.001):
            try:
                with UnlockAttempt(device) as attempt:
                    attempt.activate()
                    await fd50.send_packet(
                        device, TuyaBLECode.FUN_SENDER_DPS_V4,
                        bytes(5) + bytes((71,)), 0, True,
                    )
            except TimeoutError:
                assert failure != "success"
        item = attempt_history(device)[0]
        assert item["phase"] == ("writing_packets" if failure == "write" else "waiting_transport_ack")
        assert item["packets_written"] == (failure != "write")
        assert item["transport_ack_received"] == (failure == "success")
        assert not device._input_expected_responses
        assert not device._fd50_write_headers
    asyncio.run(run())


def test_cancelled_queued_attempt_does_not_replace_running_attempt(tmp_path):
    async def run():
        entity, _, _, _ = local_unlock_entity(tmp_path)
        device = entity._device
        device._dlock_command_lock = asyncio.Lock()
        async with device._dlock_command_lock:
            with UnlockAttempt(device) as first:
                first.activate()
                task = asyncio.create_task(DLOCKUnlockTestButton.async_press(entity))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert device._dlock_active_unlock_attempt is first
        assert attempt_history(device)[1]["status"] == "cancelled"
        assert attempt_history(device)[1]["phase"] == "queued"
    asyncio.run(run())
