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

    return {
        "connected": device.connected,
        "category": device.category,
        "product_id": device.product_id,
        "rssi": device.rssi,
        "datapoints": datapoints,
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
