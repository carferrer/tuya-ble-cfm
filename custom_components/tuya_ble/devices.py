"""Device definitions for the supported CFM Tuya BLE locks."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo, EntityDescription, generate_entity_id
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from home_assistant_bluetooth import BluetoothServiceInfoBleak

from .cloud import HASSTuyaBLEDeviceManager
from .const import DEVICE_DEF_MANUFACTURER, DOMAIN, SET_DISCONNECTED_DELAY
from .tuya_ble import (
    AbstaractTuyaBLEDeviceManager,
    TuyaBLEDataPoint,
    TuyaBLEDevice,
    TuyaBLEDeviceCredentials,
)

_LOGGER = logging.getLogger(__name__)

PRODUCT_B3AOULUH = "b3aouluh"
DP_GET_RECORDS = 69
DP_GET_RECORDS_REQUEST_ACTION = 0x01
MOBILE_CENTRAL_ID = b"\xff\xff"
INITIAL_MOBILE_RANDOM = bytes(8)


@dataclass
class TuyaBLEProductInfo:
    """Supported product information."""

    name: str
    manufacturer: str = DEVICE_DEF_MANUFACTURER


class TuyaBLEEntity(CoordinatorEntity):
    """Tuya BLE base entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: TuyaBLECoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        description: EntityDescription,
        entity_domain: str = "sensor",
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._coordinator = coordinator
        self._device = device
        self._product = product
        if description.translation_key is None:
            self._attr_translation_key = description.key
        self.entity_description = description
        self._attr_has_entity_name = True
        self._attr_device_info = get_device_info(self._device)
        self._attr_unique_id = f"{self._device.device_id}-{description.key}"
        # HA resolves existing IDs by (domain, platform, unique_id), preserving
        # user renames and automation references. Only the provisional ID changes;
        # the old sensor prefix was already replaced by HA during registration.
        self.entity_id = generate_entity_id(
            f"{entity_domain}.{{}}", self._attr_unique_id, hass=hass
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._coordinator.connected

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class TuyaBLECoordinator(DataUpdateCoordinator[None]):
    """Coordinate updates received from the BLE transport."""

    def __init__(self, hass: HomeAssistant, device: TuyaBLEDevice) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self._device = device
        self._disconnected = True
        self.last_connected_at: float | None = None
        self._unsub_disconnect: CALLBACK_TYPE | None = None
        self._dp69_response_client = None
        self._device._cfm_received_dp_events = []
        self._device._cfm_dp69_request_count = 0
        self._device._cfm_dp69_response_attempt_count = 0
        self._device._cfm_dp69_response_count = 0
        self._device._cfm_dp69_last_request = None
        self._device._cfm_dp69_last_response = None
        self._device._cfm_dp69_last_error = None
        device.register_connected_callback(self._async_handle_connect)
        device.register_callback(self._async_handle_update)
        device.register_disconnected_callback(self._async_handle_disconnect)

    @property
    def connected(self) -> bool:
        return not self._disconnected

    @callback
    def _async_handle_connect(self) -> None:
        if self._unsub_disconnect is not None:
            self._unsub_disconnect()
            self._unsub_disconnect = None
        if not self._device.connected:
            return
        self.last_connected_at = time.time()
        if self._disconnected:
            self._disconnected = False
        self.async_update_listeners()

    async def _async_reply_dp69_cached_records(
        self,
        datapoint: TuyaBLEDataPoint,
        request_value: bytes,
        client,
    ) -> None:
        """Tell a b3 lock to report cached records after a DP69 request."""
        peripheral_id = request_value[:2]
        response = (
            MOBILE_CENTRAL_ID
            + peripheral_id
            + INITIAL_MOBILE_RANDOM
            + b"\x00"
        )
        self._device._cfm_dp69_response_attempt_count += 1
        self._device._cfm_dp69_last_response = response.hex()
        self._device._cfm_dp69_last_error = None

        try:
            _LOGGER.debug(
                "%s: DP69 cached-record request %s; replying %s",
                self._device.address,
                request_value.hex(),
                response.hex(),
            )
            await datapoint.set_value(response)
        except Exception as err:  # noqa: BLE001 - diagnostic experiment
            # Permit one retry if the same GATT session reports DP69 again.
            if self._dp69_response_client is client:
                self._dp69_response_client = None
            self._device._cfm_dp69_last_error = f"{type(err).__name__}: {err}"
            _LOGGER.exception(
                "%s: Failed to reply to DP69 cached-record request",
                self._device.address,
            )
        else:
            self._device._cfm_dp69_response_count += 1

    @callback
    def _async_handle_update(self, updates: list[TuyaBLEDataPoint]) -> None:
        """Capture and propagate BLE datapoint updates to Home Assistant."""
        received_event = {
            "received_at": time.time(),
            "gatt_connected": self._device.connected,
            "datapoints": [
                {
                    "id": datapoint.id,
                    "type": datapoint.type.name,
                    "value": (
                        datapoint.value.hex()
                        if isinstance(datapoint.value, bytes)
                        else datapoint.value
                    ),
                    "timestamp": datapoint.timestamp,
                    "flags": datapoint.flags,
                }
                for datapoint in updates
            ],
        }
        self._device._cfm_received_dp_events.append(received_event)
        del self._device._cfm_received_dp_events[:-100]

        if self._device.product_id == PRODUCT_B3AOULUH:
            for datapoint in updates:
                value = datapoint.value
                if not (
                    datapoint.id == DP_GET_RECORDS
                    and datapoint.type.name == "DT_RAW"
                    and isinstance(value, bytes)
                    and len(value) == 3
                    and value[2] == DP_GET_RECORDS_REQUEST_ACTION
                ):
                    continue

                self._device._cfm_dp69_request_count += 1
                self._device._cfm_dp69_last_request = value.hex()

                client = getattr(self._device, "_client", None)
                if (
                    client is not None
                    and client.is_connected
                    and self._dp69_response_client is not client
                ):
                    # Reply at most once per real GATT session. Idle disconnects
                    # suppress coordinator disconnect callbacks, so client object
                    # identity is more reliable than a boolean reset flag here.
                    self._dp69_response_client = client
                    self.hass.async_create_task(
                        self._async_reply_dp69_cached_records(
                            datapoint,
                            bytes(value),
                            client,
                        ),
                        "Tuya BLE CFM DP69 cached-record response",
                    )

        if self._disconnected:
            self._async_handle_connect()
        self.async_set_updated_data(None)

    @callback
    def _set_disconnected(self, _: None) -> None:
        self._disconnected = True
        self._unsub_disconnect = None
        self.async_update_listeners()

    @callback
    def _async_handle_disconnect(self) -> None:
        if self._unsub_disconnect is None:
            self._unsub_disconnect = async_call_later(
                self.hass,
                float(SET_DISCONNECTED_DELAY),
                self._set_disconnected,
            )


@dataclass
class TuyaBLEData:
    title: str
    device: TuyaBLEDevice
    product: TuyaBLEProductInfo
    manager: HASSTuyaBLEDeviceManager
    coordinator: TuyaBLECoordinator


@dataclass
class TuyaBLECategoryInfo:
    products: dict[str, TuyaBLEProductInfo]
    info: TuyaBLEProductInfo | None = None


# Deliberately limited to the two product IDs physically used and tested.
devices_database: dict[str, TuyaBLECategoryInfo] = {
    "ms": TuyaBLECategoryInfo(
        products={
            "okkyfgfs": TuyaBLEProductInfo(name="P196_V Smart Lock"),
        },
    ),
    "jtmspro": TuyaBLECategoryInfo(
        products={
            "b3aouluh": TuyaBLEProductInfo(name="Smart Lock"),
        },
    ),
}


def get_product_info_by_ids(
    category: str, product_id: str
) -> TuyaBLEProductInfo | None:
    category_info = devices_database.get(category)
    if category_info is None:
        return None
    return category_info.products.get(product_id) or category_info.info


def get_device_product_info(device: TuyaBLEDevice) -> TuyaBLEProductInfo | None:
    return get_product_info_by_ids(device.category, device.product_id)


def get_short_address(address: str) -> str:
    results = address.replace("-", ":").upper().split(":")
    return f"{results[-3]}{results[-2]}{results[-1]}"[-6:]


async def get_device_readable_name(
    discovery_info: BluetoothServiceInfoBleak,
    manager: AbstaractTuyaBLEDeviceManager | None,
) -> str:
    credentials: TuyaBLEDeviceCredentials | None = None
    product_info: TuyaBLEProductInfo | None = None
    if manager:
        credentials = await manager.get_device_credentials(discovery_info.address)
        if credentials:
            product_info = get_product_info_by_ids(
                credentials.category,
                credentials.product_id,
            )
    short_address = get_short_address(discovery_info.address)
    if product_info:
        return f"{product_info.name} {short_address}"
    if credentials:
        return f"{credentials.device_name} {short_address}"
    return f"{discovery_info.device.name} {short_address}"


def get_device_info(device: TuyaBLEDevice) -> DeviceInfo:
    product_info = get_device_product_info(device)
    product_name = product_info.name if product_info else device.name
    return DeviceInfo(
        connections={(dr.CONNECTION_BLUETOOTH, device.address)},
        hw_version=device.hardware_version,
        identifiers={(DOMAIN, device.address)},
        manufacturer=(
            product_info.manufacturer if product_info else DEVICE_DEF_MANUFACTURER
        ),
        model=f"{device.product_model or product_name} ({device.product_id})",
        name=f"{product_name} {get_short_address(device.address)}",
        sw_version=f"{device.device_version} (protocol {device.protocol_version})",
    )
