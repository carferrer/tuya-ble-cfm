"""Explicit experimental writes for DLOCK-X1; never used by legacy products."""

from __future__ import annotations

import asyncio
from struct import pack

from bleak_retry_connector import BleakError

from .const import TuyaBLECode, TuyaBLEDataPointType
from .exceptions import TuyaBLEDeviceError
from .fd50 import send_packet


def command_lock(device):
    lock = getattr(device, "_dlock_command_lock", None)
    if lock is None:
        lock = device._dlock_command_lock = asyncio.Lock()
    return lock


async def write_dp(device, dp_id, dp_type, value: bytes) -> None:
    """Send one whitelisted V4 KLV; a transport ACK is not a physical result."""
    if device.product_id != "99gv5nmz":
        raise ValueError("DLOCK-X1 control used for another product")
    if not (
        (dp_id == 31 and dp_type == TuyaBLEDataPointType.DT_ENUM
         and value in (b"\x00", b"\x01", b"\x02", b"\x03"))
        or (dp_id == 33 and dp_type == TuyaBLEDataPointType.DT_BOOL
            and value in (b"\x00", b"\x01"))
        or (dp_id == 71 and dp_type == TuyaBLEDataPointType.DT_RAW and len(value) == 19)
    ):
        raise ValueError("Unsupported DLOCK-X1 write")
    if not device.connected:
        await device.reconnect()
        if not device.connected:
            raise BleakError("DLOCK-X1: no authenticated BLE connection")
        await asyncio.sleep(0.5)
    if getattr(device, "_expected_disconnect", False):
        raise BleakError("DLOCK-X1: disconnect pending")
    sn = (getattr(device, "_dlock_dp_sequence", 0) + 1) & 0xFFFFFFFF
    device._dlock_dp_sequence = sn
    data = pack(">BI", 0, sn) + pack(">BBH", dp_id, dp_type.value, len(value)) + value
    touch = getattr(device, "_lock_power_saver_touch", None)
    if touch is not None:
        touch(15.0)
    # No optimistic datapoint-cache update, retries, DP6, or inferred access event.
    await send_packet(
        device, TuyaBLECode.FUN_SENDER_DPS_V4, data, 0, True,
        expected_client=device._client,
    )


def handle_write_response(device, response_to, data: bytes) -> None:
    """Validate the echoed version/DP sequence and the SDK six-byte status."""
    expected = getattr(device, "_fd50_write_headers", {}).get(response_to)
    future = device._input_expected_responses.get(response_to)
    if expected is None or future is None or future.done():
        return
    if len(data) != 6 or data[:5] != expected:
        future.set_exception(BleakError("DLOCK-X1: invalid V4 write acknowledgement"))
    elif data[5] != 0:
        future.set_exception(TuyaBLEDeviceError(data[5]))
    else:
        future.set_result(0)
