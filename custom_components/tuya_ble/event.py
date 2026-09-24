"""Event entities for local Tuya BLE lock access records."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store

from .access import (
    ACCESS_EVENT_TYPES,
    ACCESS_STORE_MAX_KEYS,
    ACCESS_STORE_VERSION,
    access_record_from_datapoint,
    access_record_key,
    access_store_key,
    iter_access_records_from_history,
)
from .connection_policy import setup_connection_policy
from .const import DOMAIN
from .devices import PRODUCT_B3AOULUH, TuyaBLEData, get_device_info
from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDevice


class TuyaBLEAccessEvent(EventEntity):
    """Expose lock access records as Home Assistant events."""

    _attr_has_entity_name = True
    _attr_name = "Access"
    _attr_event_types = ACCESS_EVENT_TYPES

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
        self._seen_keys: list[str] = []
        self._seen_set: set[str] = set()
        self._last_record: dict[str, Any] | None = None
        self._ready = False
        self._queued_records: list[dict[str, Any]] = []
        self._attr_unique_id = f"{device.device_id}-access"
        self._attr_device_info = get_device_info(device)

    def _storage_payload(self) -> dict[str, Any]:
        """Return persistent access deduplication state."""
        return {
            "seen_keys": list(self._seen_keys),
            "last_record": (
                None if self._last_record is None else dict(self._last_record)
            ),
        }

    def _remember_record(self, record: dict[str, Any]) -> bool:
        """Remember a record and return whether it was new."""
        key = access_record_key(record)
        if key in self._seen_set:
            return False

        self._seen_keys.append(key)
        self._seen_set.add(key)
        while len(self._seen_keys) > ACCESS_STORE_MAX_KEYS:
            removed = self._seen_keys.pop(0)
            self._seen_set.discard(removed)

        if (
            self._last_record is None
            or record["event_timestamp"] >= self._last_record["event_timestamp"]
        ):
            self._last_record = dict(record)

        return True

    @callback
    def _emit_record(self, record: dict[str, Any]) -> None:
        """Emit one normalized lock access event."""
        event_data = {
            key: value
            for key, value in record.items()
            if key not in {"event_type", "event_timestamp", "received_timestamp"}
        }
        self._trigger_event(record["event_type"], event_data)
        self.async_write_ha_state()

    @callback
    def _process_record(self, record: dict[str, Any], *, emit: bool = True) -> None:
        """Deduplicate, persist and optionally emit one access record."""
        if not self._remember_record(record):
            return

        self._store.async_delay_save(self._storage_payload, 1.0)
        if emit:
            self._emit_record(record)

    @callback
    def _handle_updates(self, updates: list[TuyaBLEDataPoint]) -> None:
        """Handle live or replayed access datapoints from the BLE transport."""
        for datapoint in updates:
            record = access_record_from_datapoint(datapoint)
            if record is None:
                continue
            if not self._ready:
                self._queued_records.append(record)
                continue
            self._process_record(record)

    async def async_added_to_hass(self) -> None:
        """Load deduplication state and subscribe to Tuya BLE records."""
        await super().async_added_to_hass()
        self.async_on_remove(self._device.register_callback(self._handle_updates))

        stored = await self._store.async_load()
        history = list(
            iter_access_records_from_history(
                getattr(self._device, "_cfm_received_dp_events", [])
            )
        )

        if stored is None:
            # First installation: establish a baseline from already cached lock
            # history without replaying old access records into automations.
            for record in history:
                self._remember_record(record)
            await self._store.async_save(self._storage_payload())
        else:
            raw_keys = stored.get("seen_keys", [])
            self._seen_keys = [key for key in raw_keys if isinstance(key, str)][
                -ACCESS_STORE_MAX_KEYS:
            ]
            self._seen_set = set(self._seen_keys)
            stored_last = stored.get("last_record")
            if isinstance(stored_last, dict):
                self._last_record = stored_last

            # Records received during startup may represent real activity while
            # Home Assistant was down, so replay only those not already seen.
            for record in history:
                self._process_record(record)

        self._ready = True
        queued = self._queued_records
        self._queued_records = []
        for record in queued:
            self._process_record(record)


async def _async_connection_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Reload the device when its BLE connection policy changes."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up connection policy and access event entities."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]

    unsubscribe = setup_connection_policy(hass, entry, data.device)
    if unsubscribe is not None:
        entry.async_on_unload(unsubscribe)
    entry.async_on_unload(entry.add_update_listener(_async_connection_options_updated))

    if data.device.product_id != PRODUCT_B3AOULUH:
        return

    async_add_entities([TuyaBLEAccessEvent(hass, entry, data.device)])
