"""Buttons for the supported CFM Tuya BLE locks."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .devices import TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo
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

mapping = {
    "ms": {"okkyfgfs": LOCK_BUTTONS},
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
        super().__init__(hass, coordinator, device, product, mapping.description)
        self._mapping = mapping

    def press(self) -> None:
        """Trigger DP6 Bluetooth unlock."""
        datapoint = self._device.datapoints.get_or_create(
            self._mapping.dp_id,
            TuyaBLEDataPointType.DT_BOOL,
            False,
        )
        self._hass.create_task(datapoint.set_value(not bool(datapoint.value)))


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
    async_add_entities(entities)
