"""Experimental DLOCK-X1 controls, isolated from the proven lock families."""

from __future__ import annotations

import asyncio
import time

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.components.select import SelectEntityDescription
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.storage import Store

from .devices import TuyaBLEEntity
from .la_t01_unlock import build_dp71_unlock
from .select import TuyaBLELASelect, TuyaBLESelectMapping
from .tuya_ble import TuyaBLEDataPointType
from .tuya_ble.fd50_commands import command_lock, write_dp

VOLUME_TIMEOUT = 10
VOLUME_MAPPING = TuyaBLESelectMapping(
    dp_id=31,
    description=SelectEntityDescription(
        key="beep_volume",
        name="Volumen (experimental)",
        options=["mute", "low", "normal", "high"],
        entity_category=EntityCategory.CONFIG,
    ),
)


class DLOCKVolumeSelect(TuyaBLELASelect):
    """Reuse report-only restoration, with a DLOCK-specific command and store."""

    def __init__(self, hass, data, entry_id):
        super().__init__(
            hass, data.coordinator, data.device, data.product, VOLUME_MAPPING, entry_id
        )
        self._store = Store(hass, 1, f"tuya_ble.dlock_volume.{entry_id}")

    async def async_select_option(self, option: str) -> None:
        if self._device.product_id != "99gv5nmz":
            raise HomeAssistantError("Control exclusivo de DLOCK-X1")
        if option not in self._attr_options:
            raise HomeAssistantError("Volumen no válido")
        async with command_lock(self._device):
            value = self._attr_options.index(option)
            confirmation = asyncio.get_running_loop().create_future()
            self._confirmation = confirmation
            self._desired_value = value
            try:
                await write_dp(
                    self._device, 31, TuyaBLEDataPointType.DT_ENUM, bytes((value,))
                )
                await asyncio.wait_for(confirmation, VOLUME_TIMEOUT)
            except TimeoutError as err:
                raise HomeAssistantError(
                    "No se recibió confirmación DP31; comprueba el sonido y descarga "
                    "el diagnóstico antes de repetir"
                ) from err
            finally:
                if not confirmation.done():
                    confirmation.cancel()
                self._confirmation = None
                self._desired_value = None


class DLOCKUnlockTestButton(TuyaBLEEntity, ButtonEntity):
    """Attempt DP71 only when this device has a compatible successful report."""

    def __init__(self, hass, data):
        super().__init__(
            hass, data.coordinator, data.device, data.product,
            ButtonEntityDescription(
                key="bluetooth_unlock_dp71_test",
                name="Prueba abrir DP71 (Tuya Cloud)",
            ),
            "button",
        )
        self._manager = data.manager

    @property
    def available(self):
        return True

    async def async_press(self):
        if self._device.product_id != "99gv5nmz":
            raise HomeAssistantError("Control exclusivo de DLOCK-X1")
        async with command_lock(self._device):
            try:
                properties = await self._manager.get_lock_unlock_properties(
                    self._device.device_id
                )
                # Reuse the strict format validator, not LA-T01's BLE/control path.
                # DLOCK compatibility is experimental; never invent a missing seed.
                command = build_dp71_unlock(properties, int(time.time()))
            except ValueError as err:
                raise HomeAssistantError(
                    "No hay un informe DP71 compatible para esta DLOCK-X1. "
                    "Abre desde la app Tuya y comprueba sus propiedades DP19/DP71"
                ) from err
            await write_dp(self._device, 71, TuyaBLEDataPointType.DT_RAW, command)
