"""Persist the LA-T01 DP71 verification seed for offline use after restart.

The seed can open the lock. Keep it in Home Assistant's private storage and
never include it in logs, entity attributes, or diagnostics.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .la_t01_unlock import dp71_command_from_seed, dp71_seed_from_command
from .tuya_ble import TuyaBLEDevice

STORE_VERSION = 1


class LA_T01OfflineSession:
    """Retain the mobile DP71 verification seed for one configured lock."""

    def __init__(self, hass: HomeAssistant, entry_id: str, device: TuyaBLEDevice) -> None:
        self._hass = hass
        self._device = device
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"tuya_ble.la_t01_unlock_{entry_id}"
        )
        self._write_lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Restore a seed only when it belongs to this configured lock."""
        stored = await self._store.async_load()
        if not isinstance(stored, dict) or stored.get("device_id") != self._device.device_id:
            return
        try:
            command = dp71_command_from_seed(stored.get("seed"), int(time.time()))
        except ValueError:
            return
        self._device._cfm_dp71_local_command = command
        self._device._cfm_dp71_local_persisted = True

    async def async_save_command(self, command: bytes) -> None:
        """Save the minimum DP71 seed before the cloud-backed BLE write."""
        seed = dp71_seed_from_command(command)
        async with self._write_lock:
            await self._store.async_save(
                {"device_id": self._device.device_id, "seed": seed}
            )
            self._device._cfm_dp71_local_command = command
            self._device._cfm_dp71_local_persisted = True

    async def async_clear(self) -> None:
        """Forget a seed after pairing changes or a failed DP71 reply."""
        self._device._cfm_dp71_local_command = None
        self._device._cfm_dp71_local_persisted = False
        async with self._write_lock:
            await self._store.async_save({})

    def invalidate(self) -> None:
        """Forget in memory immediately and schedule the disk update."""
        self._device._cfm_dp71_local_command = None
        self._device._cfm_dp71_local_persisted = False
        self._hass.async_create_task(
            self.async_clear(), "Tuya BLE clear LA-T01 offline unlock seed"
        )
