"""Lock entity backed by each supported lock's motor state and DP6 command."""

from __future__ import annotations

from homeassistant.components.lock import LockEntity, LockEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .button import async_press_bluetooth_unlock
from .const import DOMAIN
from .devices import PRODUCT_B3AOULUH, PRODUCT_OKKYFGFS, TuyaBLEData, TuyaBLEEntity
from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDataPointType

DP_MOTOR_STATE = 47


class TuyaBLEMotorLock(TuyaBLEEntity, LockEntity):
    """Expose the last reported motor state as a Home Assistant lock."""

    def __init__(self, hass: HomeAssistant, data: TuyaBLEData) -> None:
        super().__init__(
            hass,
            data.coordinator,
            data.device,
            data.product,
            LockEntityDescription(key="motor_lock", name="Cerradura"),
            "lock",
        )
        self._attr_is_locked: bool | None = None

    @property
    def available(self) -> bool:
        """The command may connect on demand; retain the last reported state."""
        return True

    @callback
    def _handle_updates(self, updates: list[TuyaBLEDataPoint]) -> None:
        """Only a device report may change the displayed motor state."""
        for dp in updates:
            if (
                dp.id == DP_MOTOR_STATE
                and dp.type == TuyaBLEDataPointType.DT_BOOL
                and type(dp.value) is bool
            ):
                self._attr_is_locked = not dp.value
                self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._device.register_callback(self._handle_updates))
        lock_state = getattr(self._device, "_cfm_lock_state", None)
        if lock_state is not None:
            value = lock_state.get(DP_MOTOR_STATE)
            if value is not None:
                self._attr_is_locked = not value
            return
        dp = self._device.datapoints[DP_MOTOR_STATE]
        if (
            dp is not None
            and dp.type == TuyaBLEDataPointType.DT_BOOL
            and type(dp.value) is bool
        ):
            self._attr_is_locked = not dp.value

    async def async_lock(self, **kwargs) -> None:
        """Send the same DP6 command as the existing Open button."""
        await async_press_bluetooth_unlock(self._device)

    async def async_unlock(self, **kwargs) -> None:
        """Send the same DP6 command as the existing Open button."""
        await async_press_bluetooth_unlock(self._device)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    if data.device.product_id in (PRODUCT_B3AOULUH, PRODUCT_OKKYFGFS):
        async_add_entities([TuyaBLEMotorLock(hass, data)])
