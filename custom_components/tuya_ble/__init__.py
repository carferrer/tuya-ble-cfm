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

PRODUCT_B3AOULUH = "b3aouluh"
PRODUCT_OKKYFGFS = "okkyfgfs"

# b3aouluh normally emits isolated fast bursts even while untouched. A physical
# action produces repeated bursts much closer together, so require two complete
# bursts within a short window before opening GATT.
B3_ACTIVITY_QUIET_INTERVAL = 4.0
B3_ACTIVITY_FAST_INTERVAL = 0.7
B3_ACTIVITY_REQUIRED_FAST_INTERVALS = 3
B3_ACTIVITY_SECOND_BURST_WINDOW = 8.0

# okkyfgfs advertises much more sparsely and often loses one or more packets at
# the observed signal level. Use a longer quiet period and only two fast packets
# as a cadence fallback. Distinct payload changes are handled separately as a
# stronger wake signal for this product.
OKKY_ACTIVITY_QUIET_INTERVAL = 15.0
OKKY_ACTIVITY_FAST_INTERVAL = 0.7
OKKY_ACTIVITY_REQUIRED_FAST_INTERVALS = 2

# Activity-triggered connections only need to remain up long enough to read the
# current state and catch immediate physical-action notifications. Normal HA
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

    enable_lock_power_saver(device)

    product_info = get_device_product_info(device)
    if product_info is None:
        raise ConfigEntryNotReady(
            f"Unsupported Tuya BLE lock {device.category}/{device.product_id}"
        )

    coordinator = TuyaBLECoordinator(hass, device)

    # Keep the initial update behaviour of the hardware-tested implementation.
    hass.add_job(device.update())

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
    device._cfm_last_activity_trigger_reason = None
    device._cfm_b3_first_burst_time = None
    device._cfm_payload_activity_pending = False
    device._cfm_payload_change_count = 0

    async def _refresh_after_activity(reason: str) -> None:
        """Open GATT briefly and reliably request current DPs after activity."""
        try:
            _LOGGER.debug(
                "%s: BLE activity detected (%s); reconnecting for lock refresh",
                device.address,
                reason,
            )

            await device.reconnect()
            await asyncio.sleep(CFM_ACTIVITY_GATT_SETTLE_DELAY)
            await device.update()
            await asyncio.sleep(CFM_ACTIVITY_SECOND_REFRESH_DELAY)
            await device.update()
            device._cfm_activity_refresh_count += 1

            power_saver_touch = getattr(device, "_lock_power_saver_touch", None)
            if power_saver_touch is not None:
                power_saver_touch(CFM_ACTIVITY_IDLE_DISCONNECT_DELAY)
        except Exception:  # noqa: BLE001 - keep scanner callback resilient
            _LOGGER.exception(
                "%s: Failed to refresh lock state after BLE activity (%s)",
                device.address,
                reason,
            )
        finally:
            device._cfm_activity_update_in_progress = False

    @callback
    def _start_activity_refresh(reason: str, advertisement_time: float) -> bool:
        """Schedule exactly one activity refresh while disconnected."""
        if device.connected or device._cfm_activity_update_in_progress:
            return False

        device._cfm_activity_update_in_progress = True
        device._cfm_activity_armed = False
        device._cfm_activity_fast_streak = 0
        device._cfm_activity_trigger_count += 1
        device._cfm_last_activity_trigger_time = advertisement_time
        device._cfm_last_activity_trigger_reason = reason
        hass.async_create_task(
            _refresh_after_activity(reason),
            f"Tuya BLE CFM lock activity refresh ({reason})",
        )
        return True

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
        previous_fingerprint = device._cfm_last_advertisement_fingerprint
        fingerprint_changed = (
            previous_fingerprint is not None and fingerprint != previous_fingerprint
        )

        if fingerprint != previous_fingerprint:
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

            # On okkyfgfs the diagnostic shows very sparse advertising and real
            # fingerprint changes (manufacturer data present/absent). Preserve
            # those changes as a strong wake candidate instead of relying only
            # on a three-packet burst that this product rarely emits.
            if device.product_id == PRODUCT_OKKYFGFS and fingerprint_changed:
                device._cfm_payload_activity_pending = True
                device._cfm_payload_change_count += 1

            _LOGGER.debug(
                "%s: distinct BLE advertisement: service_data=%s manufacturer_data=%s",
                device.address,
                service_data,
                manufacturer_data,
            )

        device.set_ble_device_and_advertisement_data(
            service_info.device, advertisement
        )
        device._decode_advertisement_data()

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            BluetoothCallbackMatcher({ADDRESS: address}),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

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
        trigger_reason = None

        # okkyfgfs: a distinct payload change is more reliable than cadence at
        # the observed weak signal level. Handle it on the sampler so we do not
        # open GATT directly inside Home Assistant's Bluetooth callback.
        if device.product_id == PRODUCT_OKKYFGFS and device._cfm_payload_activity_pending:
            device._cfm_payload_activity_pending = False
            if _start_activity_refresh("payload_change", advertisement_time):
                triggered = True
                trigger_reason = "payload_change"

        if not triggered and delta is not None:
            if device.product_id == PRODUCT_OKKYFGFS:
                quiet_interval = OKKY_ACTIVITY_QUIET_INTERVAL
                fast_interval = OKKY_ACTIVITY_FAST_INTERVAL
                required_fast = OKKY_ACTIVITY_REQUIRED_FAST_INTERVALS
            else:
                quiet_interval = B3_ACTIVITY_QUIET_INTERVAL
                fast_interval = B3_ACTIVITY_FAST_INTERVAL
                required_fast = B3_ACTIVITY_REQUIRED_FAST_INTERVALS

            if delta >= quiet_interval:
                device._cfm_activity_fast_streak = 0
                if not device.connected and not device._cfm_activity_update_in_progress:
                    device._cfm_activity_armed = True
            elif delta < fast_interval:
                if (
                    device._cfm_activity_armed
                    and not device.connected
                    and not device._cfm_activity_update_in_progress
                ):
                    device._cfm_activity_fast_streak += 1
                    if device._cfm_activity_fast_streak >= required_fast:
                        if device.product_id == PRODUCT_B3AOULUH:
                            # One burst is normal for b3aouluh. Only reconnect if
                            # a second complete burst follows within 8 seconds.
                            first_burst = device._cfm_b3_first_burst_time
                            if (
                                first_burst is not None
                                and advertisement_time - first_burst
                                <= B3_ACTIVITY_SECOND_BURST_WINDOW
                            ):
                                device._cfm_b3_first_burst_time = None
                                if _start_activity_refresh(
                                    "double_burst", advertisement_time
                                ):
                                    triggered = True
                                    trigger_reason = "double_burst"
                            else:
                                device._cfm_b3_first_burst_time = advertisement_time
                                device._cfm_activity_armed = False
                                device._cfm_activity_fast_streak = 0
                        else:
                            if _start_activity_refresh(
                                "sparse_burst", advertisement_time
                            ):
                                triggered = True
                                trigger_reason = "sparse_burst"
            else:
                device._cfm_activity_fast_streak = 0

        if device.connected:
            device._cfm_activity_fast_streak = 0
            device._cfm_activity_armed = False
            device._cfm_payload_activity_pending = False
            if device.product_id == PRODUCT_B3AOULUH:
                device._cfm_b3_first_burst_time = None

        device._cfm_advertisement_events.append(
            {
                "observed_at": time.time(),
                "advertisement_time": advertisement_time,
                "delta_ms": None if delta is None else round(delta * 1000, 1),
                "product_id": device.product_id,
                "gatt_connected": device.connected,
                "activity_armed": device._cfm_activity_armed,
                "fast_streak": device._cfm_activity_fast_streak,
                "burst_candidate_pending": bool(
                    device._cfm_b3_first_burst_time is not None
                ),
                "activity_triggered": triggered,
                "trigger_reason": trigger_reason,
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
