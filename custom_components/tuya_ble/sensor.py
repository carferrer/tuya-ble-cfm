"""Sensors for the supported CFM Tuya BLE locks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .access import (
    ACCESS_STORE_VERSION,
    access_record_from_datapoint,
    access_record_value,
    access_store_key,
    newest_access_record_from_history,
)
from .const import DOMAIN
from .devices import (
    PRODUCT_B3AOULUH,
    TuyaBLEData,
    TuyaBLEEntity,
    TuyaBLEProductInfo,
    get_device_info,
)
from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDataPointType, TuyaBLEDevice

SIGNAL_STRENGTH_DP_ID = -1


@dataclass
class TuyaBLESensorMapping:
    dp_id: int
    description: SensorEntityDescription
    force_add: bool = True
    dp_type: TuyaBLEDataPointType | None = None
    getter: Callable[["TuyaBLESensor"], None] | None = None
    coefficient: float = 1.0


@dataclass
class TuyaBLEBatteryMapping(TuyaBLESensorMapping):
    description: SensorEntityDescription = field(
        default_factory=lambda: SensorEntityDescription(
            key="battery",
            name="Battery",
            device_class=SensorDeviceClass.BATTERY,
            native_unit_of_measurement=PERCENTAGE,
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.MEASUREMENT,
        )
    )


ALARM_OPTIONS = [
    "wrong_finger",
    "wrong_password",
    "wrong_card",
    "wrong_face",
    "tongue_bad",
    "too_hot",
    "unclosed_time",
    "tongue_not_out",
    "pry",
    "key_in",
    "low_battery",
    "power_off",
    "shock",
]


def alarm_mapping() -> TuyaBLESensorMapping:
    return TuyaBLESensorMapping(
        dp_id=21,
        description=SensorEntityDescription(
            key="alarm_lock",
            device_class=SensorDeviceClass.ENUM,
            options=ALARM_OPTIONS,
        ),
    )


mapping = {
    "ms": {
        "okkyfgfs": [
            alarm_mapping(),
            TuyaBLEBatteryMapping(dp_id=8),
        ]
    },
    "jtmspro": {
        "b3aouluh": [
            alarm_mapping(),
            TuyaBLESensorMapping(
                dp_id=9,
                description=SensorEntityDescription(
                    key="battery_state",
                    device_class=SensorDeviceClass.ENUM,
                    entity_category=EntityCategory.DIAGNOSTIC,
                    options=["high", "medium", "low"],
                ),
            ),
        ]
    },
}


def rssi_getter(sensor: "TuyaBLESensor") -> None:
    sensor._attr_native_value = sensor._device.rssi


rssi_mapping = TuyaBLESensorMapping(
    dp_id=SIGNAL_STRENGTH_DP_ID,
    description=SensorEntityDescription(
        key="signal_strength",
        name="Signal Strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    getter=rssi_getter,
)


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLESensorMapping]:
    return mapping.get(device.category, {}).get(device.product_id, [])


class TuyaBLESensor(TuyaBLEEntity, SensorEntity):
    """Representation of a Tuya BLE lock sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLESensorMapping,
    ) -> None:
        super().__init__(hass, coordinator, device, product, mapping.description)
        self._mapping = mapping

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._mapping.getter is not None:
            self._mapping.getter(self)
        else:
            datapoint = self._device.datapoints[self._mapping.dp_id]
            if datapoint:
                if datapoint.type == TuyaBLEDataPointType.DT_ENUM:
                    if self.entity_description.options is not None:
                        if 0 <= datapoint.value < len(self.entity_description.options):
                            self._attr_native_value = self.entity_description.options[
                                datapoint.value
                            ]
                        else:
                            self._attr_native_value = datapoint.value
                elif datapoint.type == TuyaBLEDataPointType.DT_VALUE:
                    self._attr_native_value = (
                        datapoint.value / self._mapping.coefficient
                    )
                else:
                    self._attr_native_value = datapoint.value
        self.async_write_ha_state()


class TuyaBLELastAccessSensor(SensorEntity):
    """Show when the most recent validated lock access actually occurred."""

    _attr_has_entity_name = True
    _attr_name = "Last access"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: TuyaBLEDevice,
    ) -> None:
        self._device = device
        self._store: Store[dict[str, Any]] = Store(
            hass,
            ACCESS_STORE_VERSION,
            access_store_key(entry.entry_id),
        )
        self._ready = False
        self._queued_records: list[dict[str, Any]] = []
        self._last_event_timestamp: float | None = None
        self._attr_unique_id = f"{device.device_id}-last_access"
        self._attr_device_info = get_device_info(device)

    @callback
    def _apply_record(self, record: dict[str, Any], *, write_state: bool = True) -> None:
        """Apply a normalized access record if it is newer than current state."""
        event_timestamp = float(record["event_timestamp"])
        if (
            self._last_event_timestamp is not None
            and event_timestamp < self._last_event_timestamp
        ):
            return

        access_value = access_record_value(record)
        self._last_event_timestamp = event_timestamp
        self._attr_native_value = datetime.fromtimestamp(event_timestamp, UTC)
        self._attr_extra_state_attributes = {
            "method": record["method"],
            "access_value": access_value,
            # Legacy alias retained so existing automations do not break.
            "member_id": int(record.get("member_id", access_value)),
            "dp_id": record["dp_id"],
            "event_time": record["event_time"],
            "received_at": record["received_at"],
            "delay_seconds": record["delay_seconds"],
            "recovered": record["recovered"],
        }
        if write_state:
            self.async_write_ha_state()

    @callback
    def _handle_updates(self, updates: list[TuyaBLEDataPoint]) -> None:
        """Update the timestamp sensor from live or replayed lock records."""
        for datapoint in updates:
            record = access_record_from_datapoint(datapoint)
            if record is None:
                continue
            if not self._ready:
                self._queued_records.append(record)
                continue
            self._apply_record(record)

    async def async_added_to_hass(self) -> None:
        """Restore the last access and subscribe to future access records."""
        await super().async_added_to_hass()
        self.async_on_remove(self._device.register_callback(self._handle_updates))

        stored = await self._store.async_load()
        stored_record = None if stored is None else stored.get("last_record")
        if isinstance(stored_record, dict):
            self._apply_record(stored_record, write_state=False)

        history_record = newest_access_record_from_history(
            getattr(self._device, "_cfm_received_dp_events", [])
        )
        if history_record is not None:
            self._apply_record(history_record, write_state=False)

        self._ready = True
        queued = self._queued_records
        self._queued_records = []
        for record in queued:
            self._apply_record(record, write_state=False)

        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    mappings = get_mapping_by_device(data.device)
    entities: list[SensorEntity] = [
        TuyaBLESensor(
            hass,
            data.coordinator,
            data.device,
            data.product,
            rssi_mapping,
        )
    ]
    entities.extend(
        TuyaBLESensor(
            hass,
            data.coordinator,
            data.device,
            data.product,
            item,
        )
        for item in mappings
        if item.force_add or data.device.datapoints.has_id(item.dp_id, item.dp_type)
    )
    if data.device.product_id == PRODUCT_B3AOULUH:
        entities.append(TuyaBLELastAccessSensor(hass, entry, data.device))
    async_add_entities(entities)
