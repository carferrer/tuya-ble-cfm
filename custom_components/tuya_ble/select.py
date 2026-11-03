"""Select entities for the supported CFM Tuya BLE locks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .devices import (
    PRODUCT_8GZA4O8A,
    TuyaBLEData,
    TuyaBLEEntity,
    TuyaBLEProductInfo,
)
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice


@dataclass
class TuyaBLESelectMapping:
    dp_id: int
    description: SelectEntityDescription
    force_add: bool = True
    dp_type: TuyaBLEDataPointType | None = None


LOCK_VOLUME = [
    TuyaBLESelectMapping(
        dp_id=31,
        description=SelectEntityDescription(
            key="beep_volume",
            options=["mute", "low", "normal", "high"],
            entity_category=EntityCategory.CONFIG,
        ),
    )
]

mapping = {
    "ms": {"okkyfgfs": LOCK_VOLUME},
    "jtmspro": {"b3aouluh": LOCK_VOLUME, PRODUCT_8GZA4O8A: LOCK_VOLUME},
}


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLESelectMapping]:
    return mapping.get(device.category, {}).get(device.product_id, [])


class TuyaBLESelect(TuyaBLEEntity, SelectEntity):
    """Representation of a Tuya BLE lock select."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLESelectMapping,
    ) -> None:
        super().__init__(
            hass, coordinator, device, product, mapping.description, "select"
        )
        self._mapping = mapping
        self._attr_options = mapping.description.options

    @property
    def current_option(self) -> str | None:
        datapoint = self._device.datapoints[self._mapping.dp_id]
        if datapoint:
            value = datapoint.value
            if isinstance(value, int) and 0 <= value < len(self._attr_options):
                return self._attr_options[value]
            return str(value)
        return None

    def select_option(self, value: str) -> None:
        if value in self._attr_options:
            int_value = self._attr_options.index(value)
            datapoint = self._device.datapoints.get_or_create(
                self._mapping.dp_id,
                TuyaBLEDataPointType.DT_ENUM,
                int_value,
            )
            self._hass.create_task(datapoint.set_value(int_value))


class TuyaBLELASelect(TuyaBLESelect):
    """Set LA-T01 volume over BLE, connecting first when it is asleep."""

    @property
    def available(self) -> bool:
        return True

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            raise HomeAssistantError(f"Unsupported lock volume: {option}")
        if not self._device.connected:
            await self._device.reconnect()
            if not self._device.connected:
                raise HomeAssistantError("La cerradura no estableció una conexión BLE")
            await asyncio.sleep(0.5)
        datapoint = self._device.datapoints.get_or_create(
            self._mapping.dp_id,
            TuyaBLEDataPointType.DT_ENUM,
            self._attr_options.index(option),
        )
        if datapoint.type != TuyaBLEDataPointType.DT_ENUM:
            raise HomeAssistantError("Se esperaba DP31 de tipo enum")
        await datapoint.set_value(self._attr_options.index(option))
        self.async_write_ha_state()
        touch = getattr(self._device, "_lock_power_saver_touch", None)
        if touch is not None:
            touch(5.0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    entities = [
        (TuyaBLELASelect if data.device.product_id == PRODUCT_8GZA4O8A else TuyaBLESelect)(
            hass,
            data.coordinator,
            data.device,
            data.product,
            item,
        )
        for item in get_mapping_by_device(data.device)
        if item.force_add or data.device.datapoints.has_id(item.dp_id, item.dp_type)
    ]
    async_add_entities(entities)
