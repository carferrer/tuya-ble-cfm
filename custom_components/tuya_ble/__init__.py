"""The Tuya BLE integration, trimmed for the supported CFM locks."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time

from bleak_retry_connector import get_device

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval

from .tuya_ble import TuyaBLEDevice

from .cloud import HASSTuyaBLEDeviceManager
from .const import DOMAIN
from .devices import TuyaBLECoordinator, TuyaBLEData, get_device_product_info
from .lock_power_saver import enable_lock_power_saver

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
]

# Hardware-observed pattern on b3aouluh: while idle, advertisements are usually
# separated by several seconds; physical activity produces sustained sub-second
# advertising. Use a stricter pattern than the first experiment to avoid locks
# reconnecting on normal advertising noise.
CFM_ACTIVITY_QUIET_INTERVAL = 4.0
CFM_ACTIVITY_FAST_INTERVAL = 0.7
CFM_ACTIVITY_REQUIRED_FAST_INTERVALS = 3

# Activity-triggered connections only need to remain up long enough to read the
# current state and catch the immediate physical-action notifications. Normal HA
# commands continue to use the 30 s power-saver timeout.
CFM_ACTIVITY_GATT_SETTLE_DELAY = 0.5
CFM_ACTIVITY_SECOND_REFRESH_DELAY = 0.75
CFM_ACTIVITY_IDLE_DISCONNECT_DELAY = 8.0

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tuya BLE from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    ble_device = bluetooth.async_ble_device_from_address(
        hass, address.upper(), True
    ) or await get_device(address)
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find Tuya BLE device with address {address}"
        )

    manager = HASSTuyaBLEDeviceManager(hass, entry.options.copy())
    device = TuyaBLEDevice(manager, ble_device)
    await device.initialize()

    # Proven behaviour on the user's battery-powered locks: disconnect after
    # inactivity and reconnect only when needed.
    enable_lock_power_saver(device)

    product_info = get_device_product_info(device)
    if product_info is None:
        raise ConfigEntryNotReady(
            f"Unsupported Tuya BLE lock {device.category}/{device.product_id}"
        )

    coordinator = TuyaBLECoordinator(hass, device)

    # Keep the initial update behaviour of the hardware-tested implementation.
    hass.add_job(device.update())

    # Home Assistant deliberately suppresses callback delivery for byte-for-byte
    # identical advertisements. Keep the distinct payload history from the
    # callback, and separately sample async_last_service_info() to observe the
    # timestamp of repeated packets without opening a GATT connection.
    device._cfm_advertisement_history = []
    device._cfm_advertisement_events = []
    device._cfm_last_advertisement_fingerprint = None
    device._cfm_last_seen_advertisement_time = None
    device._cfm_activity_armed = False
    device._cfm_activity_fast_streak = 0
    device._cfm_activity_update_in_progress = False
    device._cfm_activity_trigger_count = 0
    device._cfm_activity_refresh_count = 0
    device._cfm_last_activity_trigger_time = None

    @callback
    def _async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Refresh BLE device/advertisement information."""
        advertisement = service_info.advertisement
        now = time.time()

        service_data = {
            str(uuid): value.hex()
            for uuid, value in sorted(advertisement.service_data.items())
        }
        manufacturer_data = {
            f"0x{company_id:04X}": value.hex()
            for company_id, value in sorted(advertisement.manufacturer_data.items())
        }
        fingerprint = (
            tuple(service_data.items()),
            tuple(manufacturer_data.items()),
        )

        if fingerprint != device._cfm_last_advertisement_fingerprint:
            device._cfm_last_advertisement_fingerprint = fingerprint
            device._cfm_advertisement_history.append(
                {
                    "timestamp": now,
                    "rssi": advertisement.rssi,
                    "service_data": service_data,
                    "manufacturer_data": manufacturer_data,
                }
            )
            del device._cfm_advertisement_history[:-30]
            _LOGGER.debug(
                "%s: distinct BLE advertisement: service_data=%s manufacturer_data=%s",
                device.address,
                service_data,
                manufacturer_data,
            )

        device.set_ble_device_and_advertisement_data(
            service_info.device, advertisement
        )
        # Passive parsing only; this does not connect to the lock.
        device._decode_advertisement_data()

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            BluetoothCallbackMatcher({ADDRESS: address}),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    async def _refresh_after_activity() -> None:
        """Open GATT briefly and reliably request current DPs after activity."""
        device._cfm_activity_update_in_progress = True
        try:
            _LOGGER.debug(
                "%s: BLE activity burst detected; reconnecting for lock refresh",
                device.address,
            )

            # Establish notifications/pairing first, then allow the GATT session
            # to settle before asking for state. A second status request catches
            # devices that do not report DP47 on the first request immediately
            # after reconnecting.
            await device.reconnect()
            await asyncio.sleep(CFM_ACTIVITY_GATT_SETTLE_DELAY)
            await device.update()
            await asyncio.sleep(CFM_ACTIVITY_SECOND_REFRESH_DELAY)
            await device.update()
            device._cfm_activity_refresh_count += 1

            # This connection was opened only because of physical activity; keep
            # it briefly for follow-up notifications, then save battery. If a
            # normal HA command occurs, its own packet will restore the normal
            # 30-second idle timer.
            power_saver_touch = getattr(device, "_lock_power_saver_touch", None)
            if power_saver_touch is not None:
                power_saver_touch(CFM_ACTIVITY_IDLE_DISCONNECT_DELAY)
        except Exception:  # noqa: BLE001 - keep scanner callback resilient
            _LOGGER.exception(
                "%s: Failed to refresh lock state after BLE activity burst",
                device.address,
            )
        finally:
            device._cfm_activity_update_in_progress = False

    @callback
    def _sample_last_advertisement(_now) -> None:
        """Sample repeated advertisements and detect physical lock activity."""
        service_info = bluetooth.async_last_service_info(
            hass, address, connectable=True
        )
        if service_info is None:
            return

        advertisement_time = service_info.time
        previous = device._cfm_last_seen_advertisement_time
        if previous == advertisement_time:
            return

        device._cfm_last_seen_advertisement_time = advertisement_time
        delta = None if previous is None else advertisement_time - previous
        triggered = False

        if delta is not None:
            if delta >= CFM_ACTIVITY_QUIET_INTERVAL:
                # A genuine quiet gap arms the detector. The stricter 4-second
                # threshold avoids the false reconnects seen with 2.5 seconds.
                device._cfm_activity_fast_streak = 0
                if not device.connected and not device._cfm_activity_update_in_progress:
                    device._cfm_activity_armed = True
            elif delta < CFM_ACTIVITY_FAST_INTERVAL:
                if (
                    device._cfm_activity_armed
                    and not device.connected
                    and not device._cfm_activity_update_in_progress
                ):
                    device._cfm_activity_fast_streak += 1
                    if (
                        device._cfm_activity_fast_streak
                        >= CFM_ACTIVITY_REQUIRED_FAST_INTERVALS
                    ):
                        triggered = True
                        device._cfm_activity_armed = False
                        device._cfm_activity_fast_streak = 0
                        device._cfm_activity_trigger_count += 1
                        device._cfm_last_activity_trigger_time = advertisement_time
                        hass.async_create_task(
                            _refresh_after_activity(),
                            "Tuya BLE CFM lock activity refresh",
                        )
            else:
                # A medium interval breaks a candidate fast sequence but does
                # not disarm the detector; another fast sequence may still be
                # part of the same physical activity window.
                device._cfm_activity_fast_streak = 0

        if device.connected:
            device._cfm_activity_fast_streak = 0
            device._cfm_activity_armed = False

        device._cfm_advertisement_events.append(
            {
                "advertisement_time": advertisement_time,
                "delta_ms": None if delta is None else round(delta * 1000, 1),
                "activity_armed": device._cfm_activity_armed,
                "fast_streak": device._cfm_activity_fast_streak,
                "activity_triggered": triggered,
            }
        )
        del device._cfm_advertisement_events[:-200]

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _sample_last_advertisement,
            timedelta(milliseconds=250),
        )
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = TuyaBLEData(
        entry.title,
        device,
        product_info,
        manager,
        coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _async_stop(event: Event) -> None:
        """Close the connection."""
        await device.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    if entry.title != data.title:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data: TuyaBLEData = hass.data[DOMAIN].pop(entry.entry_id)
        await data.device.stop()

    return unload_ok
