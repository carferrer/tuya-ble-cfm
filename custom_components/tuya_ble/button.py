"""Buttons for the supported CFM Tuya BLE locks."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .devices import (
    PRODUCT_7A4XVBTT,
    PRODUCT_8GZA4O8A,
    PRODUCT_B3AOULUH,
    PRODUCT_OKKYFGFS,
    TuyaBLEData,
    TuyaBLEEntity,
    TuyaBLEProductInfo,
)
from .la_t01_unlock import build_dp71_unlock, refresh_dp71_unlock
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice


@dataclass
class TuyaBLEButtonMapping:
    dp_id: int
    description: ButtonEntityDescription
    force_add: bool = True
    dp_type: TuyaBLEDataPointType | None = None


LOCK_BUTTONS = [
    TuyaBLEButtonMapping(
        dp_id=6,
        description=ButtonEntityDescription(key="bluetooth_unlock"),
    )
]
UNLOCK_PULSE_SECONDS = 0.5
UNLOCK_CONNECT_SETTLE_SECONDS = 0.5
RAW_UNLOCK_MEMBER_1_PAYLOAD = b"\x01\x01"


async def async_press_bluetooth_unlock(device: TuyaBLEDevice) -> None:
    """Send only the DP6 sequence selected for this lock product."""
    command_lock = getattr(device, "_cfm_unlock_command_lock", None)
    if command_lock is None:
        command_lock = device._cfm_unlock_command_lock = asyncio.Lock()

    async with command_lock:
        if device.product_id == PRODUCT_B3AOULUH:
            datapoint = device.datapoints.get_or_create(
                6, TuyaBLEDataPointType.DT_BOOL, False
            )
            await datapoint.set_value(True)
            await asyncio.sleep(UNLOCK_PULSE_SECONDS)
            await datapoint.set_value(False)
        elif device.product_id in (
            PRODUCT_OKKYFGFS,
            PRODUCT_7A4XVBTT,
        ):
            # DP6 uses 0x01 + member ID. ID 1 is proven on both products.
            datapoint = device.datapoints.get_or_create(
                6, TuyaBLEDataPointType.DT_RAW, b"\x00\x01"
            )
            if datapoint.type != TuyaBLEDataPointType.DT_RAW:
                raise HomeAssistantError("Expected a raw DP6 on this lock")
            if device.product_id == PRODUCT_7A4XVBTT and not device.connected:
                await device.reconnect()
                if not device.connected:
                    raise HomeAssistantError("Lock did not establish a paired BLE connection")
                await asyncio.sleep(UNLOCK_CONNECT_SETTLE_SECONDS)
            await datapoint.set_value(RAW_UNLOCK_MEMBER_1_PAYLOAD)
            if device.product_id == PRODUCT_OKKYFGFS:
                await asyncio.sleep(UNLOCK_PULSE_SECONDS)
                await datapoint.set_value(RAW_UNLOCK_MEMBER_1_PAYLOAD)
        else:
            raise HomeAssistantError("Unsupported DP6 unlock product")

mapping = {
    "ms": {"okkyfgfs": LOCK_BUTTONS, PRODUCT_7A4XVBTT: LOCK_BUTTONS},
    "jtmspro": {"b3aouluh": LOCK_BUTTONS},
}


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLEButtonMapping]:
    return mapping.get(device.category, {}).get(device.product_id, [])


class TuyaBLEButton(TuyaBLEEntity, ButtonEntity):
    """Representation of a Tuya BLE lock button."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLEButtonMapping,
    ) -> None:
        super().__init__(
            hass, coordinator, device, product, mapping.description, "button"
        )
        self._mapping = mapping

    @property
    def available(self) -> bool:
        """Allow the command to establish a BLE connection on demand."""
        return True

    async def async_press(self) -> None:
        """Send this product's DP6 unlock command."""
        await async_press_bluetooth_unlock(self._device)


class TuyaBLERefreshButton(TuyaBLEEntity, ButtonEntity):
    """Connect on demand and request the lock's current datapoints."""

    def __init__(self, hass: HomeAssistant, data: TuyaBLEData) -> None:
        super().__init__(
            hass,
            data.coordinator,
            data.device,
            data.product,
            ButtonEntityDescription(key="refresh_status", name="Actualizar cerradura"),
            "button",
        )

    @property
    def available(self) -> bool:
        """Allow manual refresh precisely when the lock is disconnected."""
        return True

    async def async_press(self) -> None:
        """Use the normal paired BLE path without changing connection policy."""
        await self._device.reconnect_and_update()
        touch = getattr(self._device, "_lock_power_saver_touch", None)
        if touch is not None:
            touch(5.0)


class TuyaBLEDP71TestButton(TuyaBLEEntity, ButtonEntity):
    """Try one verified DP71 unlock on the LA-T01 only."""

    def __init__(self, hass: HomeAssistant, data: TuyaBLEData) -> None:
        super().__init__(
            hass,
            data.coordinator,
            data.device,
            data.product,
            ButtonEntityDescription(
                key="bluetooth_unlock_dp71_test", name="Preparar y abrir (Tuya Cloud)"
            ),
            "button",
        )
        self._manager = data.manager

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        """Fetch the latest successful report and send a fresh DP71 request once."""
        if self._device.product_id != PRODUCT_8GZA4O8A:
            raise HomeAssistantError("DP71 de prueba solo está disponible para LA-T01")

        command_lock = getattr(self._device, "_cfm_unlock_command_lock", None)
        if command_lock is None:
            command_lock = self._device._cfm_unlock_command_lock = asyncio.Lock()
        async with command_lock:
            try:
                properties = await self._manager.get_lock_unlock_properties(
                    self._device.device_id
                )
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err
            datapoint = self._device.datapoints.get_or_create(
                71, TuyaBLEDataPointType.DT_RAW, b""
            )
            if datapoint.type != TuyaBLEDataPointType.DT_RAW:
                raise HomeAssistantError("Se esperaba DP71 de tipo raw")
            if not self._device.connected:
                await self._device.reconnect()
                if not self._device.connected:
                    raise HomeAssistantError("La cerradura no estableció una conexión BLE")
                await asyncio.sleep(UNLOCK_CONNECT_SETTLE_SECONDS)
            try:
                command = build_dp71_unlock(properties, int(time.time()))
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err
            # Save before sending: a failed device reply can then invalidate
            # both memory and disk in the coordinator, even if it arrives early.
            session = getattr(self._device, "_cfm_dp71_session", None)
            if session is not None:
                await session.async_save_command(command)
            else:
                self._device._cfm_dp71_local_command = command
            try:
                await datapoint.set_value(command)
            except BaseException:
                if session is not None:
                    await session.async_clear()
                else:
                    self._device._cfm_dp71_local_command = None
                raise


class TuyaBLEDP71LocalTestButton(TuyaBLEEntity, ButtonEntity):
    """Try a local LA-T01 unlock using the last cloud-derived DP71 command."""

    def __init__(self, hass: HomeAssistant, data: TuyaBLEData) -> None:
        super().__init__(
            hass,
            data.coordinator,
            data.device,
            data.product,
            ButtonEntityDescription(
                key="bluetooth_unlock_dp71_local_test",
                name="Abrir (Bluetooth local)",
            ),
            "button",
        )

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        """Send one DP71 command over BLE without querying Tuya Cloud."""
        if self._device.product_id != PRODUCT_8GZA4O8A:
            raise HomeAssistantError("DP71 local solo está disponible para LA-T01")

        command_lock = getattr(self._device, "_cfm_unlock_command_lock", None)
        if command_lock is None:
            command_lock = self._device._cfm_unlock_command_lock = asyncio.Lock()
        async with command_lock:
            cached = getattr(self._device, "_cfm_dp71_local_command", None)
            if cached is None:
                raise HomeAssistantError(
                    "Primero pulsa «Preparar y abrir (Tuya Cloud)» con Internet"
                )
            try:
                command = refresh_dp71_unlock(cached, int(time.time()))
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err
            datapoint = self._device.datapoints.get_or_create(
                71, TuyaBLEDataPointType.DT_RAW, b""
            )
            if datapoint.type != TuyaBLEDataPointType.DT_RAW:
                raise HomeAssistantError("Se esperaba DP71 de tipo raw")
            if not self._device.connected:
                await self._device.reconnect()
                if not self._device.connected:
                    raise HomeAssistantError("La cerradura no estableció una conexión BLE")
                await asyncio.sleep(UNLOCK_CONNECT_SETTLE_SECONDS)
            await datapoint.set_value(command)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    entities = [
        TuyaBLEButton(
            hass,
            data.coordinator,
            data.device,
            data.product,
            item,
        )
        for item in get_mapping_by_device(data.device)
        if item.force_add or data.device.datapoints.has_id(item.dp_id, item.dp_type)
    ]
    entities.append(TuyaBLERefreshButton(hass, data))
    if data.device.product_id == PRODUCT_8GZA4O8A:
        registry = er.async_get(hass)
        obsolete_dp6 = registry.async_get_entity_id(
            "button", DOMAIN, f"{data.device.device_id}-bluetooth_unlock_dp6_test"
        )
        if obsolete_dp6 is not None:
            registry.async_remove(obsolete_dp6)
        entities.append(TuyaBLEDP71TestButton(hass, data))
        entities.append(TuyaBLEDP71LocalTestButton(hass, data))
    async_add_entities(entities)
