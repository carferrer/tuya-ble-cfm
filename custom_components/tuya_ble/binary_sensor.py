"""Binary sensors for the supported CFM Tuya BLE locks."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .devices import TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice


@dataclass
class TuyaBLEBinarySensorMapping:
    dp_id: int
    description: BinarySensorEntityDescription
    force_add: bool = True
    dp_type: TuyaBLEDataPointType | None = None


LOCK_MOTOR = [
    TuyaBLEBinarySensorMapping(
        dp_id=47,
        description=BinarySensorEntityDescription(key="lock_motor_state"),
    )
]

mapping = {
    "ms": {"okkyfgfs": LOCK_MOTOR},
    "jtmspro": {"b3aouluh": LOCK_MOTOR},
}


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLEBinarySensorMapping]:
    return mapping.get(device.category, {}).get(device.product_id, [])


class TuyaBLEBinarySensor(TuyaBLEEntity, BinarySensorEntity):
    """Representation of a Tuya BLE lock binary sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLEBinarySensorMapping,
    ) -> None:
        super().__init__(hass, coordinator, device, product, mapping.description)
        self._mapping = mapping

    @callback
    def _handle_coordinator_update(self) -> None:
        datapoint = self._device.datapoints[self._mapping.dp_id]
        if datapoint:
            self._attr_is_on = bool(datapoint.value)
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    entities = [
        TuyaBLEBinarySensor(
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
