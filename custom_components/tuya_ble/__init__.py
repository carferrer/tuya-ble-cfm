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
    Platform.EVENT,
    Platform.LOCK,
    Platform.SWITCH,
]

PRODUCT_B3AOULUH = "b3aouluh"
PRODUCT_OKKYFGFS = "okkyfgfs"


def _platforms_for_device(device: TuyaBLEDevice) -> list[Platform]:
    """Load the experimental DP33 control only for the b3 lock family."""
    if device.product_id == PRODUCT_B3AOULUH:
        return PLATFORMS
    return [
        platform
        for platform in PLATFORMS
        if platform not in (Platform.SWITCH, Platform.LOCK)
    ]

# b3aouluh idle advertising can mimic the short cadence previously used to
# catch event-like DP47 quickly. Cached-record recovery via DP69 means we no
# longer need to race the physical event, so require a stronger burst and keep
# a cooldown after each refresh to avoid disconnect/reconnect loops.
B3_ACTIVITY_QUIET_INTERVAL = 4.0
B3_ACTIVITY_FAST_INTERVAL = 0.7
B3_ACTIVITY_REQUIRED_FAST_INTERVALS = 3
B3_ACTIVITY_IDLE_DISCONNECT_DELAY = 3.0
B3_ACTIVITY_COOLDOWN = 30.0

# okkyfgfs advertises sparsely and often loses packets at the observed signal
# level. After a quiet period, reconnect on the first fast interval so GATT is
# established as early as possible while the physical-action event is active.
OKKY_ACTIVITY_QUIET_INTERVAL = 15.0
OKKY_ACTIVITY_FAST_INTERVAL = 0.7
OKKY_ACTIVITY_REQUIRED_FAST_INTERVALS = 1
OKKY_ACTIVITY_IDLE_DISCONNECT_DELAY = 8.0

# Notifications/pairing are established by reconnect(). Keep the refresh delays
# short so event-like DP47 data is requested while the physical event is active.
CFM_ACTIVITY_GATT_SETTLE_DELAY = 0.1
CFM_ACTIVITY_SECOND_REFRESH_DELAY = 0.5

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
    device._cfm_activity_refresh_attempt_count = 0
    device._cfm_activity_connect_count = 0
    device._cfm_last_activity_trigger_time = None
    device._cfm_last_activity_trigger_reason = None
    device._cfm_payload_activity_pending = False
    device._cfm_payload_change_count = 0
    device._cfm_last_refresh_started_at = None
    device._cfm_last_refresh_connected_at = None
    device._cfm_last_refresh_finished_at = None
    device._cfm_last_refresh_error = None
    device._cfm_last_refresh_dp47_before = None
    device._cfm_last_refresh_dp47_after = None
    device._cfm_activity_refresh_history = []

    def _b3_activity_cooldown_remaining() -> float:
        """Return remaining b3 activity cooldown in seconds."""
        if device.product_id != PRODUCT_B3AOULUH:
            return 0.0
        last_finished = device._cfm_last_refresh_finished_at
        if last_finished is None:
            return 0.0
        return max(0.0, B3_ACTIVITY_COOLDOWN - (time.time() - last_finished))

    async def _refresh_after_activity(
        reason: str, advertisement_time: float
    ) -> None:
        """Open GATT quickly and request current DPs after suspected activity."""
        device._cfm_activity_refresh_attempt_count += 1
        started_at = time.time()
        device._cfm_last_refresh_started_at = started_at
        device._cfm_last_refresh_connected_at = None
        device._cfm_last_refresh_finished_at = None
        device._cfm_last_refresh_error = None

        dp47 = device.datapoints[47]
        dp47_before = (
            None
            if dp47 is None
            else {"value": dp47.value, "timestamp": dp47.timestamp}
        )
        device._cfm_last_refresh_dp47_before = dp47_before

        refresh_record = {
            "reason": reason,
            "advertisement_time": advertisement_time,
            "started_at": started_at,
            "connected_at": None,
            "finished_at": None,
            "duration_ms": None,
            "dp47_before": dp47_before,
            "dp47_after": None,
            "error": None,
        }

        try:
            _LOGGER.debug(
                "%s: BLE activity detected (%s); reconnecting for lock refresh",
                device.address,
                reason,
            )

            await device.reconnect()
            if device.connected:
                device._cfm_activity_connect_count += 1
                connected_at = time.time()
                device._cfm_last_refresh_connected_at = connected_at
                refresh_record["connected_at"] = connected_at
            else:
                _LOGGER.warning(
                    "%s: Activity refresh reconnect returned without paired GATT",
                    device.address,
                )

            await asyncio.sleep(CFM_ACTIVITY_GATT_SETTLE_DELAY)
            await device.update()
            await asyncio.sleep(CFM_ACTIVITY_SECOND_REFRESH_DELAY)
            await device.update()
            device._cfm_activity_refresh_count += 1

            dp47 = device.datapoints[47]
            dp47_after = (
                None
                if dp47 is None
                else {"value": dp47.value, "timestamp": dp47.timestamp}
            )
            device._cfm_last_refresh_dp47_after = dp47_after
            refresh_record["dp47_after"] = dp47_after

            power_saver_touch = getattr(device, "_lock_power_saver_touch", None)
            if power_saver_touch is not None:
                idle_delay = (
                    B3_ACTIVITY_IDLE_DISCONNECT_DELAY
                    if device.product_id == PRODUCT_B3AOULUH
                    else OKKY_ACTIVITY_IDLE_DISCONNECT_DELAY
                )
                power_saver_touch(idle_delay)
        except Exception as err:  # noqa: BLE001 - keep scanner callback resilient
            error = f"{type(err).__name__}: {err}"
            device._cfm_last_refresh_error = error
            refresh_record["error"] = error
            _LOGGER.exception(
                "%s: Failed to refresh lock state after BLE activity (%s)",
                device.address,
                reason,
            )
        finally:
            finished_at = time.time()
            device._cfm_last_refresh_finished_at = finished_at
            refresh_record["finished_at"] = finished_at
            refresh_record["duration_ms"] = round(
                (finished_at - started_at) * 1000, 1
            )
            device._cfm_activity_refresh_history.append(refresh_record)
            del device._cfm_activity_refresh_history[:-30]
            device._cfm_activity_update_in_progress = False

    @callback
    def _start_activity_refresh(reason: str, advertisement_time: float) -> bool:
        """Schedule exactly one activity refresh while disconnected."""
        if device.connected or device._cfm_activity_update_in_progress:
            return False
        if _b3_activity_cooldown_remaining() > 0:
            device._cfm_activity_armed = False
            device._cfm_activity_fast_streak = 0
            return False

        device._cfm_activity_update_in_progress = True
        device._cfm_activity_armed = False
        device._cfm_activity_fast_streak = 0
        device._cfm_activity_trigger_count += 1
        device._cfm_last_activity_trigger_time = advertisement_time
        device._cfm_last_activity_trigger_reason = reason
        hass.async_create_task(
            _refresh_after_activity(reason, advertisement_time),
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
        b3_cooldown_remaining = _b3_activity_cooldown_remaining()

        if b3_cooldown_remaining > 0:
            device._cfm_activity_armed = False
            device._cfm_activity_fast_streak = 0

        # okkyfgfs: a distinct payload change is a strong wake candidate. Handle
        # it on the sampler so GATT is not opened inside HA's Bluetooth callback.
        if device.product_id == PRODUCT_OKKYFGFS and device._cfm_payload_activity_pending:
            device._cfm_payload_activity_pending = False
            if _start_activity_refresh("payload_change", advertisement_time):
                triggered = True
                trigger_reason = "payload_change"

        if not triggered and delta is not None and b3_cooldown_remaining <= 0:
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
                        reason = (
                            "burst"
                            if device.product_id == PRODUCT_B3AOULUH
                            else "sparse_burst"
                        )
                        if _start_activity_refresh(reason, advertisement_time):
                            triggered = True
                            trigger_reason = reason
            else:
                device._cfm_activity_fast_streak = 0

        if device.connected:
            device._cfm_activity_fast_streak = 0
            device._cfm_activity_armed = False
            device._cfm_payload_activity_pending = False

        device._cfm_advertisement_events.append(
            {
                "observed_at": time.time(),
                "advertisement_time": advertisement_time,
                "delta_ms": None if delta is None else round(delta * 1000, 1),
                "product_id": device.product_id,
                "gatt_connected": device.connected,
                "activity_armed": device._cfm_activity_armed,
                "fast_streak": device._cfm_activity_fast_streak,
                "activity_triggered": triggered,
                "trigger_reason": trigger_reason,
                "cooldown_remaining_ms": round(b3_cooldown_remaining * 1000, 1),
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

    await hass.config_entries.async_forward_entry_setups(
        entry, _platforms_for_device(device)
    )
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
    """Unload Tuya BLE entry."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, _platforms_for_device(data.device)
    ):
        hass.data[DOMAIN].pop(entry.entry_id)
        await data.device.stop()

    return unload_ok
