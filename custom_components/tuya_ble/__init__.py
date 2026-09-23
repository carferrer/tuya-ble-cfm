"""The Tuya BLE integration, trimmed for the supported CFM locks."""

from __future__ import annotations

import logging
import time

from bleak_retry_connector import get_device

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

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
    # inactivity, reconnect only when needed, while preserving device-originated
    # state updates handled by the legacy transport.
    enable_lock_power_saver(device)

    product_info = get_device_product_info(device)
    if product_info is None:
        raise ConfigEntryNotReady(
            f"Unsupported Tuya BLE lock {device.category}/{device.product_id}"
        )

    coordinator = TuyaBLECoordinator(hass, device)

    # Keep the initial update behaviour of the hardware-tested implementation.
    hass.add_job(device.update())

    # Diagnostic-only passive advertisement history. Keep only distinct raw
    # payloads so we can determine whether physical lock activity is encoded in
    # advertisements without opening a GATT connection or increasing battery use.
    device._cfm_advertisement_history = []
    device._cfm_last_advertisement_fingerprint = None

    @callback
    def _async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Refresh BLE device/advertisement information."""
        advertisement = service_info.advertisement
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
                    "timestamp": time.time(),
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
        # Refresh the metadata already understood by the legacy decoder. This is
        # passive parsing only; it does not connect to the lock.
        device._decode_advertisement_data()

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            BluetoothCallbackMatcher({ADDRESS: address}),
            bluetooth.BluetoothScanningMode.ACTIVE,
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
