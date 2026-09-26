"""Opt-in passage-mode experiment for the physically tested b3aouluh lock."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .devices import PRODUCT_B3AOULUH, TuyaBLEData, TuyaBLEEntity
from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDataPointType, TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)
DP_PASSAGE_MODE_CANDIDATE = 33
CONFIRM_TIMEOUT_SECONDS = 15


class TuyaBLEPassageModeSwitch(TuyaBLEEntity, SwitchEntity):
    """Test whether the DP33 state observed with the inside button is writable."""

    # Enable this only on the one lock selected for the physical experiment.
    _attr_entity_registry_enabled_default = False

    def __init__(self, hass: HomeAssistant, data: TuyaBLEData) -> None:
        super().__init__(
            hass,
            data.coordinator,
            data.device,
            data.product,
            SwitchEntityDescription(
                key="passage_mode_experimental", name="Passage mode (experimental)"
            ),
            "switch",
        )
        self._attr_is_on: bool | None = None
        self._command_lock = asyncio.Lock()
        self._confirmation: asyncio.Future[bool] | None = None
        self._desired_state: bool | None = None

    @property
    def available(self) -> bool:
        """Only offer controls after the lock has reported a Boolean DP33."""
        return self._device.datapoints.has_id(
            DP_PASSAGE_MODE_CANDIDATE, TuyaBLEDataPointType.DT_BOOL
        )

    @callback
    def _handle_updates(self, updates: list[TuyaBLEDataPoint]) -> None:
        """Use only reports received from the lock, never the optimistic send cache."""
        for dp in updates:
            if (
                dp.id != DP_PASSAGE_MODE_CANDIDATE
                or dp.type != TuyaBLEDataPointType.DT_BOOL
                or type(dp.value) is not bool
            ):
                continue
            self._attr_is_on = dp.value
            self.async_write_ha_state()
            if (
                self._confirmation is not None
                and not self._confirmation.done()
                and dp.value == self._desired_state
            ):
                self._confirmation.set_result(True)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Do not let the optimistically changed datapoint confirm a command."""
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._device.register_callback(self._handle_updates))
        dp = self._device.datapoints[DP_PASSAGE_MODE_CANDIDATE]
        if dp is not None and dp.type == TuyaBLEDataPointType.DT_BOOL:
            self._attr_is_on = bool(dp.value)

    async def _set_mode(self, desired: bool) -> None:
        if self._device.product_id != PRODUCT_B3AOULUH:
            raise HomeAssistantError("Passage-mode test is limited to b3aouluh")

        async with self._command_lock:
            dp = self._device.datapoints[DP_PASSAGE_MODE_CANDIDATE]
            if dp is None or dp.type != TuyaBLEDataPointType.DT_BOOL:
                raise HomeAssistantError("Lock has not reported a Boolean DP33")
            confirmation = asyncio.get_running_loop().create_future()
            self._confirmation = confirmation
            self._desired_state = desired
            try:
                # set_value changes the transport cache before writing. The
                # callback above accepts only a subsequent device report.
                await dp.set_value(desired)
                await asyncio.wait_for(confirmation, CONFIRM_TIMEOUT_SECONDS)
            except TimeoutError as err:
                _LOGGER.warning(
                    "%s: No DP33 report confirming passage mode %s",
                    self._device.address,
                    desired,
                )
                raise HomeAssistantError(
                    "No DP33 confirmation received; check the lock physically"
                ) from err
            finally:
                if not confirmation.done():
                    confirmation.cancel()
                self._confirmation = None
                self._desired_state = None

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_mode(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_mode(False)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    if data.device.product_id == PRODUCT_B3AOULUH:
        async_add_entities([TuyaBLEPassageModeSwitch(hass, data)])
