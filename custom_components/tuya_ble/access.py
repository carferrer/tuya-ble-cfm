"""Helpers for local Tuya BLE lock access records."""

from __future__ import annotations

from datetime import UTC, datetime
import time
from typing import Any, Iterable

from .const import DOMAIN
from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDataPointType

DP_FINGERPRINT_UNLOCK = 12
DP_PASSWORD_UNLOCK = 13
DP_DYNAMIC_PASSWORD_UNLOCK = 14
DP_CARD_UNLOCK = 15
DP_BLE_UNLOCK = 19
DP_TEMPORARY_PASSWORD_UNLOCK = 55
DP_PHONE_REMOTE_UNLOCK = 62
DP_VOICE_REMOTE_UNLOCK = 63

EVENT_FINGERPRINT_UNLOCK = "fingerprint_unlock"
EVENT_PASSWORD_UNLOCK = "password_unlock"
EVENT_DYNAMIC_PASSWORD_UNLOCK = "dynamic_password_unlock"
EVENT_CARD_UNLOCK = "card_unlock"
EVENT_BLE_UNLOCK = "ble_unlock"
EVENT_TEMPORARY_PASSWORD_UNLOCK = "temporary_password_unlock"
EVENT_PHONE_REMOTE_UNLOCK = "phone_remote_unlock"
EVENT_VOICE_REMOTE_UNLOCK = "voice_remote_unlock"

# Tuya function definition for b3aouluh declares these unlock records as
# DT_VALUE datapoints. Fingerprint (DP12) and password (DP13) have been
# physically validated; the remaining methods use the same normalized path and
# will only emit when the corresponding datapoint is actually received.
ACCESS_RECORD_TYPES = {
    DP_FINGERPRINT_UNLOCK: (EVENT_FINGERPRINT_UNLOCK, "fingerprint"),
    DP_PASSWORD_UNLOCK: (EVENT_PASSWORD_UNLOCK, "password"),
    DP_DYNAMIC_PASSWORD_UNLOCK: (EVENT_DYNAMIC_PASSWORD_UNLOCK, "dynamic_password"),
    DP_CARD_UNLOCK: (EVENT_CARD_UNLOCK, "card"),
    DP_BLE_UNLOCK: (EVENT_BLE_UNLOCK, "ble"),
    DP_TEMPORARY_PASSWORD_UNLOCK: (
        EVENT_TEMPORARY_PASSWORD_UNLOCK,
        "temporary_password",
    ),
    DP_PHONE_REMOTE_UNLOCK: (EVENT_PHONE_REMOTE_UNLOCK, "phone_remote"),
    DP_VOICE_REMOTE_UNLOCK: (EVENT_VOICE_REMOTE_UNLOCK, "voice_remote"),
}
ACCESS_EVENT_TYPES = [
    event_type for event_type, _method in ACCESS_RECORD_TYPES.values()
]
ACCESS_RECORD_REPLAY_DELAY = 2.0
ACCESS_STORE_VERSION = 1
ACCESS_STORE_MAX_KEYS = 200


def access_store_key(entry_id: str) -> str:
    """Return the per-config-entry storage key for access record deduplication."""
    return f"{DOMAIN}.access_records.{entry_id}"


def build_access_record(
    dp_id: int,
    value: int,
    event_timestamp: float,
    received_timestamp: float | None = None,
) -> dict[str, Any] | None:
    """Build a normalized access record from a supported Tuya lock datapoint."""
    record_type = ACCESS_RECORD_TYPES.get(dp_id)
    if record_type is None:
        return None
    event_type, method = record_type

    received_timestamp = (
        time.time() if received_timestamp is None else float(received_timestamp)
    )
    event_timestamp = float(event_timestamp)
    delay_seconds = max(0.0, received_timestamp - event_timestamp)

    return {
        "event_type": event_type,
        "method": method,
        "dp_id": dp_id,
        # Keep this key for storage/backwards compatibility while the exact
        # semantics of Tuya's numeric unlock value are still being validated.
        "member_id": int(value),
        "event_timestamp": event_timestamp,
        "event_time": datetime.fromtimestamp(event_timestamp, UTC).isoformat(),
        "received_timestamp": received_timestamp,
        "received_at": datetime.fromtimestamp(received_timestamp, UTC).isoformat(),
        "delay_seconds": round(delay_seconds, 3),
        "recovered": delay_seconds > ACCESS_RECORD_REPLAY_DELAY,
    }


def access_record_from_datapoint(
    datapoint: TuyaBLEDataPoint,
    received_timestamp: float | None = None,
) -> dict[str, Any] | None:
    """Convert a live Tuya datapoint to a normalized access record."""
    if (
        datapoint.id not in ACCESS_RECORD_TYPES
        or datapoint.type != TuyaBLEDataPointType.DT_VALUE
        or not isinstance(datapoint.value, int)
    ):
        return None

    return build_access_record(
        datapoint.id,
        datapoint.value,
        datapoint.timestamp,
        received_timestamp,
    )


def access_record_key(record: dict[str, Any]) -> str:
    """Return a stable key used to suppress replayed access records."""
    return (
        f"{record['dp_id']}:"
        f"{float(record['event_timestamp']):.3f}:"
        f"{record['member_id']}"
    )


def iter_access_records_from_history(
    history: Iterable[dict[str, Any]],
) -> Iterable[dict[str, Any]]:
    """Yield supported access records captured before entities were created."""
    for received_event in history:
        received_at = received_event.get("received_at")
        if not isinstance(received_at, int | float):
            continue

        for datapoint in received_event.get("datapoints", []):
            dp_id = datapoint.get("id")
            value = datapoint.get("value")
            timestamp = datapoint.get("timestamp")
            if (
                dp_id not in ACCESS_RECORD_TYPES
                or datapoint.get("type") != "DT_VALUE"
                or not isinstance(value, int)
                or not isinstance(timestamp, int | float)
            ):
                continue

            if record := build_access_record(
                dp_id,
                value,
                timestamp,
                received_at,
            ):
                yield record


def newest_access_record_from_history(
    history: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the newest supported access record in diagnostic history."""
    newest: dict[str, Any] | None = None
    for record in iter_access_records_from_history(history):
        if (
            newest is None
            or record["event_timestamp"] > newest["event_timestamp"]
        ):
            newest = record
    return newest
