"""Experimental DLOCK-X1 controls, isolated from the proven lock families."""

from __future__ import annotations

import asyncio
import time

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.components.select import SelectEntityDescription
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.storage import Store

from .devices import TuyaBLEEntity
from .dlock_unlock import (
    CONF_DLOCK_SEED, build_command, configured_seed, load_seed, private_filename, response_result,
)
from .select import TuyaBLELASelect, TuyaBLESelectMapping
from .tuya_ble import TuyaBLEDataPointType
from .tuya_ble.fd50_commands import command_lock, write_dp

VOLUME_TIMEOUT = 10
UNLOCK_TIMEOUT = 15
VOLUME_MAPPING = TuyaBLESelectMapping(
    dp_id=31,
    description=SelectEntityDescription(
        key="beep_volume",
        name="Volumen (experimental)",
        options=["mute", "low", "normal", "high"],
        entity_category=EntityCategory.CONFIG,
    ),
)


class DLOCKVolumeSelect(TuyaBLELASelect):
    """Reuse report-only restoration, with a DLOCK-specific command and store."""

    def __init__(self, hass, data, entry_id):
        super().__init__(
            hass, data.coordinator, data.device, data.product, VOLUME_MAPPING, entry_id
        )
        self._store = Store(hass, 1, f"tuya_ble.dlock_volume.{entry_id}")

    async def async_select_option(self, option: str) -> None:
        if self._device.product_id != "99gv5nmz":
            raise HomeAssistantError("Control exclusivo de DLOCK-X1")
        if option not in self._attr_options:
            raise HomeAssistantError("Volumen no válido")
        async with command_lock(self._device):
            value = self._attr_options.index(option)
            confirmation = asyncio.get_running_loop().create_future()
            self._confirmation = confirmation
            self._desired_value = value
            try:
                await write_dp(
                    self._device, 31, TuyaBLEDataPointType.DT_ENUM, bytes((value,))
                )
                await asyncio.wait_for(confirmation, VOLUME_TIMEOUT)
            except TimeoutError as err:
                raise HomeAssistantError(
                    "No se recibió confirmación DP31; comprueba el sonido y descarga "
                    "el diagnóstico antes de repetir"
                ) from err
            finally:
                if not confirmation.done():
                    confirmation.cancel()
                self._confirmation = None
                self._desired_value = None


class DLOCKUnlockTestButton(TuyaBLEEntity, ButtonEntity):
    """Send one local DP71 with this unit's privately imported verification."""

    def __init__(self, hass, data, config_entry):
        super().__init__(
            hass, data.coordinator, data.device, data.product,
            ButtonEntityDescription(
                key="bluetooth_unlock_dp71_test",
                name="Abrir cerradura",
            ),
            "button",
        )
        self._config_entry = config_entry

    @property
    def available(self):
        return True

    async def async_press(self):
        if self._device.product_id != "99gv5nmz":
            raise HomeAssistantError("Control exclusivo de DLOCK-X1")
        async with command_lock(self._device):
            try:
                if CONF_DLOCK_SEED in self._config_entry.options:
                    seed = configured_seed(
                        self._config_entry.options[CONF_DLOCK_SEED],
                        self._device.product_id, self._device.device_id, self._device.address,
                    )
                else:
                    # Compatibility with the initial private-file trial. Once
                    # configured, never fall back to possibly stale verification.
                    path = self._hass.config.path(
                        "tuya_ble_private", private_filename(self._device.address)
                    )
                    seed = await self._hass.async_add_executor_job(
                        load_seed, path, self._device.product_id,
                        self._device.device_id, self._device.address,
                    )
            except ValueError as err:
                raise HomeAssistantError(str(err)) from None
            # Generate the timestamp after connection, not before a slow handshake.
            if not self._device.connected:
                await self._device.reconnect()
                if not self._device.connected:
                    raise HomeAssistantError("No se pudo conectar con DLOCK-X1")
                await asyncio.sleep(0.5)
            command = build_command(seed, int(time.time()))
            client = self._device._client
            confirmation = asyncio.get_running_loop().create_future()

            def on_report(datapoints):
                if confirmation.done() or self._device._client is not client:
                    return
                for datapoint in datapoints:
                    if datapoint.id != 71 or datapoint.type != TuyaBLEDataPointType.DT_RAW:
                        continue
                    result = response_result(command, datapoint.value)
                    if result is not None:
                        confirmation.set_result(result)
                        return

            unsubscribe = self._device.register_callback(on_report)
            self._device._dlock_dp71_last_result = {"status": "pending"}
            try:
                await write_dp(self._device, 71, TuyaBLEDataPointType.DT_RAW, command)
                result = await asyncio.wait_for(confirmation, UNLOCK_TIMEOUT)
                self._device._dlock_dp71_last_result = {
                    "status": "confirmed" if result == 0 else "rejected",
                    "response_code": result,
                }
                if result != 0:
                    raise HomeAssistantError(
                        f"DLOCK-X1 rechazó DP71 (código {result}); no se ha reintentado"
                    )
            except TimeoutError:
                self._device._dlock_dp71_last_result = {"status": "timeout"}
                raise HomeAssistantError(
                    "No se confirmó la apertura DP71; comprueba físicamente la cerradura "
                    "antes de repetir. No se ha reintentado"
                ) from None
            except HomeAssistantError:
                raise
            except asyncio.CancelledError:
                self._device._dlock_dp71_last_result = {"status": "cancelled"}
                raise
            except Exception:
                self._device._dlock_dp71_last_result = {"status": "transport_error"}
                raise HomeAssistantError(
                    "Falló la comunicación DP71; comprueba físicamente la cerradura "
                    "antes de repetir. No se ha reintentado"
                ) from None
            finally:
                unsubscribe()
                if not confirmation.done():
                    confirmation.cancel()
