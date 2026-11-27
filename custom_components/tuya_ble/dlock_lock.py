"""Estimated DLOCK-X1 lock state; no changes to legacy motor lock entities."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

from homeassistant.components.lock import LockEntity, LockEntityDescription, LockEntityFeature
from homeassistant.exceptions import HomeAssistantError

from .access import access_record_from_datapoint, access_record_key
from .devices import TuyaBLEEntity
from .dlock_controls import DLOCKUnlockTestButton
from .tuya_ble import TuyaBLEDataPointType as DP


class DLOCKLock(TuyaBLEEntity, LockEntity):
    """Combine live opening evidence with auto-lock and actual motor reports."""

    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, hass, data, entry):
        super().__init__(
            hass, data.coordinator, data.device, data.product,
            LockEntityDescription(key="motor_lock", name="Cerradura"), "lock",
        )
        self._config_entry = entry
        self._attr_is_locked = None
        self._source = "unknown"
        self._estimated = True
        self._passage = None
        self._last_evidence = 0.0
        self._last_mode_evidence = 0.0
        self._seen = deque(maxlen=200)

    @property
    def available(self):
        return True

    @property
    def extra_state_attributes(self):
        return {
            "state_source": self._source,
            "state_estimated": self._estimated,
            "passage_mode": self._passage,
        }

    def _set_state(self, locked, source, estimated, timestamp):
        self._attr_is_locked = locked
        self._source = source
        self._estimated = estimated
        self._last_evidence = max(self._last_evidence, timestamp)
        self.async_write_ha_state()

    def _local_open(self):
        self._set_state(
            False, "passage_mode" if self._passage else "dp71", True,
            datetime.now(UTC).timestamp(),
        )

    def _handle_updates(self, updates):
        now = datetime.now(UTC).timestamp()
        for dp in updates:
            # Never use restored/cache records or delayed historical reports.
            timestamp = getattr(dp, "timestamp", None)
            if not isinstance(timestamp, (int, float)) or not 0 <= now - timestamp <= 2:
                continue
            if dp.id == 33 and dp.type == DP.DT_BOOL and type(dp.value) is bool:
                if timestamp < self._last_mode_evidence:
                    continue
                self._last_mode_evidence = timestamp
                self._passage = not dp.value
                if self._source == "startup_auto_lock" and self._passage:
                    # New mode evidence contradicts the basis of the startup guess.
                    self._attr_is_locked = None
                    self._source = "unknown"
                # A mode change is not an opening or a closing confirmation.
                if self._attr_is_locked is False:
                    self._source = "passage_mode" if self._passage else "awaiting_motor"
                    self._estimated = True
                self.async_write_ha_state()
            elif dp.id == 47 and dp.type == DP.DT_BOOL and type(dp.value) is bool:
                if timestamp < self._last_evidence:
                    continue
                if not dp.value and timestamp < self._last_mode_evidence:
                    continue
                if not dp.value and self._passage and self._attr_is_locked is False:
                    self._set_state(False, "passage_mode", True, timestamp)
                else:
                    self._set_state(not dp.value, "motor", False, timestamp)
            else:
                record = access_record_from_datapoint(dp, now)
                if record is None or record["recovered"]:
                    continue
                key = access_record_key(record)
                if key in self._seen:
                    continue
                self._seen.append(key)
                if timestamp <= self._last_evidence:
                    continue
                self._set_state(
                    False, "passage_mode" if self._passage else "access_event",
                    True, timestamp,
                )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # Only a saved auto-lock=true permits the requested startup estimate.
        # Do not restore old motor values or replay cached access events.
        state = getattr(self._device, "_cfm_lock_state", None)
        if state is not None:
            value = state.get(33)
            if type(value) is bool:
                self._passage = not value
                if value is True:
                    self._attr_is_locked = True
                    self._source = "startup_auto_lock"
                    self._estimated = True
        self.async_on_remove(self._device.register_callback(self._handle_updates))
        listeners = getattr(self._device, "_dlock_open_listeners", None)
        if listeners is None:
            listeners = self._device._dlock_open_listeners = []
        listeners.append(self._local_open)
        self.async_on_remove(lambda: listeners.remove(self._local_open))

    async def async_unlock(self, **kwargs):
        # Same configured seed, serialization, one write and confirmation as button.
        await DLOCKUnlockTestButton.async_press(self)

    async def async_open(self, **kwargs):
        await self.async_unlock(**kwargs)

    async def async_lock(self, **kwargs):
        raise HomeAssistantError(
            "DLOCK-X1 no dispone de una orden de cierre verificada. "
            "Usa cierre automático y espera la confirmación del motor"
        )
