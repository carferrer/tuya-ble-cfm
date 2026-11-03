"""Select entities for the supported CFM Tuya BLE locks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .devices import (
    PRODUCT_8GZA4O8A,
    TuyaBLEData,
    TuyaBLEEntity,
    TuyaBLEProductInfo,
)
from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDataPointType, TuyaBLEDevice


VOLUME_CONFIRM_TIMEOUT_SECONDS = 8
VOLUME_APPLY_SETTLE_SECONDS = 10.0
VOLUME_STORE_VERSION = 1


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

LA_T01_VOLUME = [
    TuyaBLESelectMapping(
        dp_id=31,
        description=SelectEntityDescription(
            key="beep_volume",
            translation_key="beep_volume_experimental",
            options=["mute", "low", "normal", "high"],
            entity_category=EntityCategory.CONFIG,
        ),
    )
]

mapping = {
    "ms": {"okkyfgfs": LOCK_VOLUME},
    "jtmspro": {"b3aouluh": LOCK_VOLUME, PRODUCT_8GZA4O8A: LA_T01_VOLUME},
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

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLESelectMapping,
        entry_id: str,
    ) -> None:
        super().__init__(hass, coordinator, device, product, mapping)
        self._store: Store[dict[str, str]] = Store(
            hass, VOLUME_STORE_VERSION, f"{DOMAIN}.la_t01_volume.{entry_id}"
        )
        self._confirmed_option: str | None = None
        self._last_saved_option: str | None = None
        self._command_lock = asyncio.Lock()
        self._confirmation: asyncio.Future[bool] | None = None
        self._desired_value: int | None = None

    @property
    def available(self) -> bool:
        return True

    @property
    def current_option(self) -> str | None:
        """Show only a DP31 value reported by the lock, not our send cache."""
        return self._confirmed_option

    @callback
    def _handle_volume_updates(self, updates: list[TuyaBLEDataPoint]) -> None:
        for dp in updates:
            if (
                dp.id != self._mapping.dp_id
                or dp.type != TuyaBLEDataPointType.DT_ENUM
                or type(dp.value) is not int
                or not 0 <= dp.value < len(self._attr_options)
            ):
                continue
            self._confirmed_option = self._attr_options[dp.value]
            self.async_write_ha_state()
            if self._confirmed_option != self._last_saved_option:
                self._last_saved_option = self._confirmed_option
                self._store.async_delay_save(
                    lambda: {"option": self._last_saved_option}, 1.0
                )
            if (
                self._confirmation is not None
                and not self._confirmation.done()
                and dp.value == self._desired_value
            ):
                self._confirmation.set_result(True)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._device.register_callback(self._handle_volume_updates))

        self._restore_last_volume_report()
        if self._confirmed_option is None:
            await self._restore_stored_volume_report()

    async def _restore_stored_volume_report(self) -> None:
        """Use the last known value when this session has no fresh DP31 report."""
        saved = await self._store.async_load()
        option = saved.get("option") if isinstance(saved, dict) else None
        if self._confirmed_option is None and option in self._attr_options:
            self._confirmed_option = option
            self._last_saved_option = option
            self.async_write_ha_state()

    @callback
    def _restore_last_volume_report(self) -> None:
        """Recover a received DP31 report without trusting the send cache."""
        # An initial BLE report can arrive before HA creates the select. Use
        # the received-event history, never the datapoint cache changed by sends.
        for event in reversed(getattr(self._device, "_cfm_received_dp_events", [])):
            for report in reversed(event.get("datapoints", [])):
                value = report.get("value")
                if (
                    report.get("id") == self._mapping.dp_id
                    and report.get("type") == "DT_ENUM"
                    and type(value) is int
                    and 0 <= value < len(self._attr_options)
                ):
                    self._confirmed_option = self._attr_options[value]
                    self._last_saved_option = self._confirmed_option
                    self._store.async_delay_save(
                        lambda: {"option": self._last_saved_option}, 1.0
                    )
                    self.async_write_ha_state()
                    return

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            raise HomeAssistantError(f"Unsupported lock volume: {option}")
        async with self._command_lock:
            if not self._device.connected:
                await self._device.reconnect()
                if not self._device.connected:
                    raise HomeAssistantError("La cerradura no estableció una conexión BLE")
                await asyncio.sleep(0.5)
            value = self._attr_options.index(option)
            datapoint = self._device.datapoints.get_or_create(
                self._mapping.dp_id, TuyaBLEDataPointType.DT_ENUM, value
            )
            if datapoint.type != TuyaBLEDataPointType.DT_ENUM:
                raise HomeAssistantError("Se esperaba DP31 de tipo enum")
            confirmation = asyncio.get_running_loop().create_future()
            self._confirmation = confirmation
            self._desired_value = value
            try:
                await datapoint.set_value(value)
                touch = getattr(self._device, "_lock_power_saver_touch", None)
                if touch is not None:
                    touch(VOLUME_APPLY_SETTLE_SECONDS + 2.0)
                if not confirmation.done():
                    await self._device.update()
                await asyncio.wait_for(confirmation, VOLUME_CONFIRM_TIMEOUT_SECONDS)
                # This LA-T01 reported DP31 immediately but applied the sound
                # only after a wait. Finish the HA action when it can be tested.
                await asyncio.sleep(VOLUME_APPLY_SETTLE_SECONDS)
            except TimeoutError as err:
                raise HomeAssistantError(
                    "La cerradura no confirmó DP31; comprueba el volumen físicamente"
                ) from err
            finally:
                if not confirmation.done():
                    confirmation.cancel()
                self._confirmation = None
                self._desired_value = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for item in get_mapping_by_device(data.device):
        if not (item.force_add or data.device.datapoints.has_id(item.dp_id, item.dp_type)):
            continue
        args = (hass, data.coordinator, data.device, data.product, item)
        if data.device.product_id == PRODUCT_8GZA4O8A:
            entities.append(TuyaBLELASelect(*args, entry.entry_id))
        else:
            entities.append(TuyaBLESelect(*args))
    async_add_entities(entities)
