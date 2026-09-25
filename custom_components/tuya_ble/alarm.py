"""Normalize timestamped DP21 lock alarm records."""

from __future__ import annotations

from datetime import UTC, datetime
import math
import time
from typing import Any, Iterable

from .const import DOMAIN
from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDataPointType

# Keep the sensor's existing enum order. Only wrong_finger and wrong_password
# have been physically validated as cached records on b3aouluh.
ALARM_OPTIONS = [
    "wrong_finger",
    "wrong_password",
    "wrong_card",
    "wrong_face",
    "tongue_bad",
    "too_hot",
    "unclosed_time",
    "tongue_not_out",
    "pry",
    "key_in",
    "low_battery",
    "power_off",
    "shock",
]
ALARM_STORE_VERSION = 1
ALARM_STORE_MAX_KEYS = 200


def alarm_store_key(entry_id: str) -> str:
    """Use storage independent of access records and other locks."""
    return f"{DOMAIN}.alarm_records.{entry_id}"


def build_alarm_record(
    value: int,
    event_timestamp: float,
    received_timestamp: float | None = None,
) -> dict[str, Any] | None:
    """Normalize known alarms, rejecting invalid enum values and timestamps."""
    if type(value) is not int or not 0 <= value < len(ALARM_OPTIONS):
        return None
    received_timestamp = (
        time.time() if received_timestamp is None else received_timestamp
    )
    if any(
        type(stamp) not in (int, float) or not math.isfinite(stamp)
        for stamp in (event_timestamp, received_timestamp)
    ):
        return None
    try:
        event_time = datetime.fromtimestamp(event_timestamp, UTC).isoformat()
        received_at = datetime.fromtimestamp(received_timestamp, UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return None
    delay = max(0.0, received_timestamp - event_timestamp)
    return {
        "event_type": ALARM_OPTIONS[value],
        "dp_id": 21,
        "alarm_value": value,
        "event_timestamp": float(event_timestamp),
        "event_time": event_time,
        "received_timestamp": float(received_timestamp),
        "received_at": received_at,
        "delay_seconds": round(delay, 3),
        "recovered": delay > 2.0,
    }


def alarm_record_from_datapoint(
    datapoint: TuyaBLEDataPoint,
    received_timestamp: float | None = None,
) -> dict[str, Any] | None:
    """Accept DP21 enums only, without interpreting access records as alarms."""
    if datapoint.id != 21 or datapoint.type != TuyaBLEDataPointType.DT_ENUM:
        return None
    return build_alarm_record(datapoint.value, datapoint.timestamp, received_timestamp)


def alarm_record_key(record: dict[str, Any]) -> str:
    """Distinguish repeated failures while suppressing replay of one record."""
    return f"21:{record['event_timestamp']:.3f}:{record['alarm_value']}"


def iter_alarm_records_from_history(
    history: Iterable[dict[str, Any]],
) -> Iterable[dict[str, Any]]:
    """Read alarm records received before entity initialization."""
    for event in history:
        received_at = event.get("received_at")
        if type(received_at) not in (int, float):
            continue
        for datapoint in event.get("datapoints", []):
            if datapoint.get("id") != 21 or datapoint.get("type") != "DT_ENUM":
                continue
            record = build_alarm_record(
                datapoint.get("value"), datapoint.get("timestamp"), received_at
            )
            if record is not None:
                yield record
