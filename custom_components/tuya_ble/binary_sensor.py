"""Binary sensors for the supported CFM Tuya BLE locks."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.entity import EntityCategory

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
        super().__init__(
            hass, coordinator, device, product, mapping.description, "binary_sensor"
        )
        self._mapping = mapping

    @property
    def available(self) -> bool:
        """Keep the last known motor state visible during BLE power saving."""
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        lock_state = getattr(self._device, "_cfm_lock_state", None)
        if lock_state is not None and self._mapping.dp_id == 47:
            self._attr_is_on = lock_state.get(47)

    @callback
    def _handle_coordinator_update(self) -> None:
        lock_state = getattr(self._device, "_cfm_lock_state", None)
        if lock_state is not None and self._mapping.dp_id == 47:
            self._attr_is_on = lock_state.get(47)
            self.async_write_ha_state()
            return
        datapoint = self._device.datapoints[self._mapping.dp_id]
        if datapoint:
            self._attr_is_on = bool(datapoint.value)
        self.async_write_ha_state()


class TuyaBLEConnectionBinarySensor(TuyaBLEEntity, BinarySensorEntity):
    """Show whether HA currently has a paired BLE session with this lock."""

    def __init__(self, hass: HomeAssistant, data: TuyaBLEData) -> None:
        super().__init__(
            hass,
            data.coordinator,
            data.device,
            data.product,
            BinarySensorEntityDescription(
                key="ble_connection",
                name="Conexión Bluetooth",
                device_class=BinarySensorDeviceClass.CONNECTIVITY,
                entity_category=EntityCategory.DIAGNOSTIC,
            ),
            "binary_sensor",
        )
        self._attr_is_on = data.device.connected

    @property
    def available(self) -> bool:
        """Disconnected is a valid state, not an unavailable entity."""
        return True

    @callback
    def _handle_connection_status(self) -> None:
        connected = self._device.connected
        if self._attr_is_on != connected:
            self._attr_is_on = connected
            self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._handle_connection_status()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._device.register_connection_status_callback(
                self._handle_connection_status
            )
        )
        self._handle_connection_status()


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
    entities.append(TuyaBLEConnectionBinarySensor(hass, data))
    async_add_entities(entities)
