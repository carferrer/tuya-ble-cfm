"""DLOCK-X1-only FD50 session negotiation; legacy transports are unchanged."""

from __future__ import annotations

import asyncio
import logging
from struct import pack
import time

from bleak_retry_connector import BleakClientWithServiceCache, BleakError, establish_connection

from .const import TuyaBLECode

_LOGGER = logging.getLogger(__name__)
WRITE_UUID = "00000001-0000-1001-8001-00805f9b07d0"
NOTIFY_UUID = "00000002-0000-1001-8001-00805f9b07d0"
CONNECT_TIMEOUT = 45
RESPONSE_TIMEOUT = 10
RETRY_COOLDOWN = 10


async def _close_failed_client(device, client) -> None:
    """Release a failed session without scheduling an automatic reconnect."""
    device._is_paired = False
    device._client = None
    if client is not None:
        try:
            async with asyncio.timeout(5):
                await client.disconnect()
        except Exception:
            _LOGGER.debug("%s: FD50 cleanup could not disconnect", device.address)


async def ensure_connected(device) -> None:
    """Make one bounded connection/handshake attempt per device."""
    if device._expected_disconnect or device.connected:
        return
    async with device._connect_lock:
        if device._expected_disconnect or device.connected:
            return
        if time.monotonic() < getattr(device, "_fd50_retry_after", 0):
            raise BleakError("DLOCK-X1: previous connection failed; wait 10 seconds")
        client = None
        stage = "connect"
        device._fd50_stage = stage
        device._fd50_last_error = None
        device._fd50_write_size = 20
        device._protocol_version = 2
        device._session_key = None
        device._is_paired = False
        device._clean_input()
        async with device._seq_num_lock:
            device._current_seq_num = 1
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                _LOGGER.debug("%s: FD50 connecting", device.address)
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    device._ble_device,
                    device.address,
                    device._disconnected,
                    use_services_cache=False,
                    ble_device_callback=lambda: device._ble_device,
                    max_attempts=1,
                )
                device._client = client
                stage = device._fd50_stage = "services"
                write = client.services.get_characteristic(WRITE_UUID)
                notify = client.services.get_characteristic(NOTIFY_UUID)
                if write is None or notify is None:
                    raise BleakError("DLOCK-X1: expected FD50 characteristics not found")
                # Use the negotiated ATT write capacity, never a guessed proxy MTU.
                size = write.max_write_without_response_size
                if not isinstance(size, int) or size < 20:
                    raise BleakError("DLOCK-X1: invalid negotiated write capacity")
                device._fd50_write_size = min(size, 244)
                _LOGGER.debug(
                    "%s: FD50 characteristics found; write capacity=%s",
                    device.address, device._fd50_write_size,
                )
                stage = device._fd50_stage = "notify"
                await client.start_notify(NOTIFY_UUID, device._notification_handler)
                stage = device._fd50_stage = "device_info"
                await send_packet(
                    device, TuyaBLECode.FUN_SENDER_DEVICE_INFO,
                    pack(">H", device._fd50_write_size), 0, True,
                )
                if device._session_key is None:
                    raise BleakError("DLOCK-X1: no session key from device info")
                if not device._is_bound:
                    raise BleakError("DLOCK-X1: device reports unbound; pair in Tuya first")
                stage = device._fd50_stage = "pair"
                await send_packet(
                    device, TuyaBLECode.FUN_SENDER_PAIR,
                    device._build_pairing_request(), 0, True,
                )
                if not device.connected:
                    raise BleakError("DLOCK-X1: BLE session was not authenticated")
        except asyncio.CancelledError:
            device._fd50_stage = "cancelled"
            await _close_failed_client(device, client)
            raise
        except Exception as err:
            # Keep diagnostics useful without copying payloads or credentials.
            device._fd50_last_error = f"{stage}: {type(err).__name__}"
            device._fd50_stage = "failed"
            device._fd50_retry_after = time.monotonic() + RETRY_COOLDOWN
            await _close_failed_client(device, client)
            _LOGGER.warning(
                "%s: FD50 connection failed at %s (%s)",
                device.address, stage, type(err).__name__,
            )
            raise BleakError(f"DLOCK-X1 FD50 failed at {stage}: {type(err).__name__}") from err
        device._fd50_stage = "ready"
        device._fire_connected_callbacks()
        device._fire_connection_status_callbacks()


async def send_packet(device, code, data, response_to, wait_for_response):
    """Send via FD50 with bounded response waits and no background retry loop."""
    seq = await device._get_seq_num()
    future = asyncio.get_running_loop().create_future() if wait_for_response else None
    if future is not None:
        device._input_expected_responses[seq] = future
    try:
        packets = device._build_packets(seq, code, data, response_to)
        async with device._operation_lock:
            client = device._client
            if client is None or not client.is_connected:
                raise BleakError("DLOCK-X1: disconnected before FD50 write")
            for packet in packets:
                await client.write_gatt_char(WRITE_UUID, packet, response=False)
        if future is not None:
            await asyncio.wait_for(future, RESPONSE_TIMEOUT)
        return True
    finally:
        device._input_expected_responses.pop(seq, None)
        if future is not None and not future.done():
            future.cancel()
