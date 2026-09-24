from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {
    "username",
    "password",
    "local_key",
    "api_key",
    "token",
    "host",
    "mac",
}


def _serialize_value(value: Any) -> Any:
    """Make Tuya datapoint values JSON serializable for diagnostics."""
    if isinstance(value, bytes):
        return value.hex()
    return value


def _runtime_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any] | None:
    """Return runtime Tuya BLE data for this config entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return None

    device = runtime.device
    datapoints: list[dict[str, Any]] = []
    raw_datapoints = getattr(device.datapoints, "_datapoints", {})
    for dp_id, datapoint in sorted(raw_datapoints.items()):
        datapoints.append(
            {
                "id": dp_id,
                "type": datapoint.type.name,
                "value": _serialize_value(datapoint.value),
                "timestamp": datapoint.timestamp,
            }
        )

    client = getattr(device, "_client", None)
    idle_task = getattr(device, "_lock_power_saver_idle_task", None)

    return {
        "connected": device.connected,
        "gatt_connected": bool(client is not None and client.is_connected),
        "paired": bool(getattr(device, "_is_paired", False)),
        "category": device.category,
        "product_id": device.product_id,
        "rssi": device.rssi,
        "datapoints": datapoints,
        "received_dp_events": list(
            getattr(device, "_cfm_received_dp_events", [])
        ),
        "record_recovery": {
            "dp69_request_count": int(
                getattr(device, "_cfm_dp69_request_count", 0)
            ),
            "dp69_response_attempt_count": int(
                getattr(device, "_cfm_dp69_response_attempt_count", 0)
            ),
            "dp69_response_count": int(
                getattr(device, "_cfm_dp69_response_count", 0)
            ),
            "dp69_last_request": getattr(
                device, "_cfm_dp69_last_request", None
            ),
            "dp69_last_response": getattr(
                device, "_cfm_dp69_last_response", None
            ),
            "dp69_last_error": getattr(
                device, "_cfm_dp69_last_error", None
            ),
        },
        "power_saver": {
            "idle_disconnect_delay": getattr(
                device, "_lock_power_saver_idle_disconnect_delay", None
            ),
            "idle_task_pending": bool(idle_task is not None and not idle_task.done()),
            "idle_disconnecting": bool(
                getattr(device, "_lock_power_saver_idle_disconnecting", False)
            ),
        },
        "connection_policy": {
            "mode": getattr(
                device,
                "_cfm_connection_mode",
                getattr(device, "_lock_connection_mode", None),
            ),
            "sync_interval_minutes": getattr(
                device, "_cfm_periodic_sync_interval_minutes", None
            ),
            "periodic_sync_attempt_count": int(
                getattr(device, "_cfm_periodic_sync_attempt_count", 0)
            ),
            "periodic_sync_success_count": int(
                getattr(device, "_cfm_periodic_sync_success_count", 0)
            ),
            "periodic_sync_last_started_at": getattr(
                device, "_cfm_periodic_sync_last_started_at", None
            ),
            "periodic_sync_last_error": getattr(
                device, "_cfm_periodic_sync_last_error", None
            ),
            "keep_alive_attempt_count": int(
                getattr(device, "_cfm_keep_alive_attempt_count", 0)
            ),
            "keep_alive_success_count": int(
                getattr(device, "_cfm_keep_alive_success_count", 0)
            ),
            "keep_alive_last_error": getattr(
                device, "_cfm_keep_alive_last_error", None
            ),
        },
        "activity_detector": {
            "armed": bool(getattr(device, "_cfm_activity_armed", False)),
            "fast_streak": int(getattr(device, "_cfm_activity_fast_streak", 0)),
            "update_in_progress": bool(
                getattr(device, "_cfm_activity_update_in_progress", False)
            ),
            "trigger_count": int(
                getattr(device, "_cfm_activity_trigger_count", 0)
            ),
            "refresh_attempt_count": int(
                getattr(device, "_cfm_activity_refresh_attempt_count", 0)
            ),
            "connect_count": int(
                getattr(device, "_cfm_activity_connect_count", 0)
            ),
            "refresh_count": int(
                getattr(device, "_cfm_activity_refresh_count", 0)
            ),
            "last_trigger_time": getattr(
                device, "_cfm_last_activity_trigger_time", None
            ),
            "last_trigger_reason": getattr(
                device, "_cfm_last_activity_trigger_reason", None
            ),
            "last_refresh_started_at": getattr(
                device, "_cfm_last_refresh_started_at", None
            ),
            "last_refresh_connected_at": getattr(
                device, "_cfm_last_refresh_connected_at", None
            ),
            "last_refresh_finished_at": getattr(
                device, "_cfm_last_refresh_finished_at", None
            ),
            "last_refresh_error": getattr(
                device, "_cfm_last_refresh_error", None
            ),
            "last_refresh_dp47_before": getattr(
                device, "_cfm_last_refresh_dp47_before", None
            ),
            "last_refresh_dp47_after": getattr(
                device, "_cfm_last_refresh_dp47_after", None
            ),
            "payload_activity_pending": bool(
                getattr(device, "_cfm_payload_activity_pending", False)
            ),
            "payload_change_count": int(
                getattr(device, "_cfm_payload_change_count", 0)
            ),
            "refresh_history": list(
                getattr(device, "_cfm_activity_refresh_history", [])
            ),
        },
        "advertisement_history": list(
            getattr(device, "_cfm_advertisement_history", [])
        ),
        "advertisement_events": list(
            getattr(device, "_cfm_advertisement_events", [])
        ),
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry):
    data: dict[str, Any] = {
        "entry": entry.as_dict(),
        "data": entry.data,
        "options": entry.options,
    }

    runtime = _runtime_diagnostics(hass, entry)
    if runtime is not None:
        data["runtime"] = runtime

    return async_redact_data(data, TO_REDACT)


async def async_get_device_diagnostics(hass: HomeAssistant, entry, device):
    device_data: dict[str, Any] = {
        "device_name": device.name,
        "identifiers": list(device.identifiers),
        "manufacturer": device.manufacturer,
        "model": device.model,
    }

    runtime = _runtime_diagnostics(hass, entry)
    if runtime is not None:
        device_data["runtime"] = runtime

    return async_redact_data(device_data, TO_REDACT)
