"""Persist the last device-reported passage and motor states per lock."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDataPointType, TuyaBLEDevice

LOCK_STATE_STORE_VERSION = 1
STORED_DPS = (33, 47)


class TuyaBLELockState:
    """Retain only reports from the device, never the outgoing DP cache."""

    def __init__(self, hass: HomeAssistant, entry_id: str, device: TuyaBLEDevice) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, LOCK_STATE_STORE_VERSION, f"tuya_ble.lock_state_{entry_id}"
        )
        self._device = device
        self._values: dict[int, bool] = {}

    async def async_load(self) -> None:
        """Load the previous confirmed values before any refresh starts."""
        stored = await self._store.async_load()
        values = stored.get("values") if isinstance(stored, dict) else None
        if isinstance(values, dict):
            for dp_id in STORED_DPS:
                value = values.get(str(dp_id))
                if type(value) is bool:
                    self._values[dp_id] = value

    def register(self) -> None:
        self._unregister = self._device.register_callback(self._handle_updates)

    def unregister(self) -> None:
        self._unregister()

    def get(self, dp_id: int) -> bool | None:
        return self._values.get(dp_id)

    @callback
    def _handle_updates(self, updates: list[TuyaBLEDataPoint]) -> None:
        changed = False
        for dp in updates:
            if (
                dp.id in STORED_DPS
                and dp.type == TuyaBLEDataPointType.DT_BOOL
                and type(dp.value) is bool
                and self._values.get(dp.id) != dp.value
            ):
                self._values[dp.id] = dp.value
                changed = True
        if changed:
            self._store.async_delay_save(
                lambda: {"values": {str(k): v for k, v in self._values.items()}},
                1.0,
            )
