"""Decode DLOCK-X1 V4 reports without changing legacy DP parsing.

Wire layout follows Tuya SDK main/sdk/src/tuya_ble_api.c
(tuya_ble_dp_data_send / tuya_ble_dp_data_with_time_send) and the
eight-byte response consumed by tuya_ble_data_handler.c.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import logging
import time

from .const import TuyaBLECode, TuyaBLEDataPointType
from .exceptions import TuyaBLEDataFormatError, TuyaBLEDataLengthError
from .fd50 import send_packet

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Report:
    timestamp: float
    flags: int
    acknowledgement: bytes | None
    datapoints: tuple


def _reject(error_type, reason: str, offset: int, **metadata):
    """Attach only explicit structural fields; never include raw DP values."""
    error = error_type()
    error.report_details = {"reason": reason, "offset": offset, **metadata}
    raise error


def parse_report(code: TuyaBLECode, data: bytes, received_at: float) -> Report:
    """Validate the entire SDK V4 payload before making any state changes."""
    if code not in (TuyaBLECode.FUN_RECEIVE_DP_V4, TuyaBLECode.FUN_RECEIVE_TIME_DP_V4):
        _reject(TuyaBLEDataFormatError, "unsupported_command", 0)
    if len(data) < 7:
        _reject(TuyaBLEDataLengthError, "truncated_report_header", 0, remaining=len(data))
    if data[0] != 0:
        _reject(TuyaBLEDataFormatError, "unsupported_header_version", 0, header_version=data[0])
    if data[5] not in (0, 1, 128, 129):
        _reject(TuyaBLEDataFormatError, "unsupported_report_flags", 5, report_flags=data[5])
    if data[6] > 3:
        _reject(TuyaBLEDataFormatError, "unsupported_report_mode", 6, report_mode=data[6])
    timestamp = received_at
    pos = 7
    if code == TuyaBLECode.FUN_RECEIVE_TIME_DP_V4:
        if len(data) < 8:
            _reject(TuyaBLEDataLengthError, "missing_timestamp_type", 7)
        time_type = data[7]
        if time_type == 1:
            pos = 12
            if len(data) < pos:
                _reject(TuyaBLEDataLengthError, "truncated_timestamp", 8,
                        timestamp_type=time_type, remaining=len(data) - 8)
            timestamp = float(int.from_bytes(data[8:12], "big"))
        elif time_type == 0:
            pos = 21
            if len(data) < pos:
                _reject(TuyaBLEDataLengthError, "truncated_timestamp", 8,
                        timestamp_type=time_type, remaining=len(data) - 8)
            raw_time = data[8:21]
            if any(byte < 48 or byte > 57 for byte in raw_time):
                _reject(TuyaBLEDataFormatError, "invalid_timestamp_digits", 8)
            timestamp = int(raw_time) / 1000
        else:
            _reject(TuyaBLEDataFormatError, "unsupported_timestamp_type", 7,
                    timestamp_type=time_type)
    if pos == len(data):
        _reject(TuyaBLEDataLengthError, "missing_datapoints", pos)
    parsed = []
    while pos < len(data):
        if len(data) - pos < 4:
            _reject(TuyaBLEDataLengthError, "truncated_dp_header", pos,
                    remaining=len(data) - pos)
        dp_id, raw_type = data[pos:pos + 2]
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        metadata = {
            "dp_id": dp_id, "dp_type": raw_type, "declared_length": length,
            "remaining": len(data) - pos - 4,
        }
        dp_offset = pos
        pos += 4
        if pos + length > len(data):
            _reject(TuyaBLEDataLengthError, "truncated_dp_value", dp_offset, **metadata)
        try:
            dp_type = TuyaBLEDataPointType(raw_type)
        except ValueError:
            _reject(TuyaBLEDataFormatError, "unknown_dp_type", dp_offset, **metadata)
        raw = data[pos:pos + length]
        pos += length
        if dp_type == TuyaBLEDataPointType.DT_BOOL:
            if length != 1:
                _reject(TuyaBLEDataFormatError, "invalid_bool_length", dp_offset, **metadata)
            if raw[0] not in (0, 1):
                _reject(TuyaBLEDataFormatError, "invalid_bool_encoding", dp_offset, **metadata)
            value = bool(raw[0])
        elif dp_type == TuyaBLEDataPointType.DT_VALUE:
            # DLOCK hardware reports DP8 battery and DP12/19 access as uint8.
            # Keep standard int32 support and reject other compact encodings.
            if length == 1 and dp_id in (8, 12, 19):
                value = raw[0]
            elif length == 4:
                value = int.from_bytes(raw, "big", signed=True)
            else:
                _reject(TuyaBLEDataFormatError, "invalid_value_length", dp_offset, **metadata)
        elif dp_type == TuyaBLEDataPointType.DT_ENUM:
            if length != 1:
                _reject(TuyaBLEDataFormatError, "invalid_enum_length", dp_offset, **metadata)
            value = raw[0]
        elif dp_type == TuyaBLEDataPointType.DT_STRING:
            try:
                value = raw.decode("utf-8")
            except UnicodeError:
                _reject(TuyaBLEDataFormatError, "invalid_string_encoding", dp_offset, **metadata)
        else:
            if dp_type == TuyaBLEDataPointType.DT_BITMAP and length not in (1, 2, 4):
                _reject(TuyaBLEDataFormatError, "invalid_bitmap_length", dp_offset, **metadata)
            value = raw
        parsed.append((dp_id, dp_type, value))
    # Preserve mode as the existing event pipeline's flags field.
    return Report(
        timestamp, data[6],
        None if data[5] & 0x80 else data[:7] + b"\x00",
        tuple(parsed),
    )


def _increment(device, name):
    setattr(device, name, getattr(device, name, 0) + 1)


async def _acknowledge(device, client, code, payload, seq_num):
    """Answer only on the session that supplied the report; consume failures."""
    try:
        if client is None:
            raise RuntimeError("Report session is no longer available")
        async with asyncio.timeout(5):
            await send_packet(device, code, payload, seq_num, False, expected_client=client)
        _increment(device, "_fd50_v4_acks_sent")
        touch = getattr(device, "_lock_power_saver_touch", None)
        if touch is not None:
            touch()
    except asyncio.CancelledError:
        raise
    except Exception as err:
        _increment(device, "_fd50_v4_ack_errors")
        _LOGGER.debug("%s: V4 ACK failed (%s)", device.address, type(err).__name__)


def handle_report(device, seq_num: int, code: TuyaBLECode, data: bytes) -> None:
    """Publish validated records in order, then acknowledge if requested."""
    if getattr(device, "product_id", None) != "99gv5nmz":
        return
    _increment(device, "_fd50_v4_received")
    try:
        report = parse_report(code, data, time.time())
    except (TuyaBLEDataLengthError, TuyaBLEDataFormatError) as err:
        _increment(device, "_fd50_v4_rejected")
        # Only structural metadata: never store raw packets or credential DPs.
        rejection = {
            "code": code.name, "length": len(data), "valid": False,
            "received_at": time.time(),
            **getattr(err, "report_details", {"reason": "invalid_report"}),
        }
        device._fd50_v4_last_report = rejection
        history = getattr(device, "_fd50_v4_rejections", None)
        if history is None:
            history = device._fd50_v4_rejections = deque(maxlen=20)
        history.append(rejection)
        _LOGGER.debug("%s: V4 report rejected: %s", device.address, rejection)
        raise
    device._fd50_v4_last_report = {
        "code": code.name, "length": len(data), "valid": True,
        "dp_ids": [dp_id for dp_id, _, _ in report.datapoints],
        "ack_requested": report.acknowledgement is not None,
    }
    _increment(device, "_fd50_v4_parsed")
    seen = getattr(device, "_fd50_v4_seen", None)
    if seen is None:
        seen = device._fd50_v4_seen = deque(maxlen=64)
    # Exact frame retransmissions in the same session should not generate a
    # new event-time for an otherwise identical report without a timestamp.
    key = (seq_num, code, data)
    if key not in seen:
        # Emit each occurrence separately, so repeated IDs retain every value.
        for dp_id, dp_type, value in report.datapoints:
            device._datapoints._update_from_device(
                dp_id, report.timestamp, report.flags, dp_type, value
            )
            device._fire_callbacks([device._datapoints[dp_id]])
        seen.append(key)
    if report.acknowledgement is not None:
        client = device._client
        tasks = getattr(device, "_fd50_report_ack_tasks", None)
        if tasks is None:
            tasks = device._fd50_report_ack_tasks = set()
        task = asyncio.create_task(
            _acknowledge(device, client, code, report.acknowledgement, seq_num)
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
