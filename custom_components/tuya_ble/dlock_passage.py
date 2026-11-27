"""DLOCK-X1 passage control confirmed by the app's V4 DP33 exchange."""

from __future__ import annotations

import asyncio

from homeassistant.exceptions import HomeAssistantError

from .switch import TuyaBLEPassageModeSwitch
from .tuya_ble import TuyaBLEDataPointType
from .tuya_ble.fd50_commands import command_lock, write_dp

CONFIRM_TIMEOUT = 15


class DLOCKPassageModeSwitch(TuyaBLEPassageModeSwitch):
    """Invert automatic lock; state/restoration use only actual DP33 reports."""

    @property
    def available(self) -> bool:
        # The app capture proves DP33 even when status queries return no DPs.
        return True

    def _passage_state(self, dp33_value: bool) -> bool:
        return not dp33_value

    async def _set_mode(self, desired: bool) -> None:
        if self._device.product_id != "99gv5nmz":
            raise HomeAssistantError("Control exclusivo de DLOCK-X1")
        async with command_lock(self._device):
            if not self._device.connected:
                await self._device.reconnect()
                if not self._device.connected:
                    raise HomeAssistantError("No se pudo conectar con DLOCK-X1")
                await asyncio.sleep(0.5)
            confirmation = asyncio.get_running_loop().create_future()
            self._confirmation = confirmation
            self._desired_state = not desired
            try:
                # Capture: false disables auto-lock; true enables it. No
                # DP71/DP70/DP46 or automatic opening follows either write.
                await write_dp(
                    self._device, 33, TuyaBLEDataPointType.DT_BOOL,
                    bytes((int(not desired),)),
                )
                await asyncio.wait_for(confirmation, CONFIRM_TIMEOUT)
            except TimeoutError:
                raise HomeAssistantError(
                    "No se confirmó DP33. Comprueba físicamente el modo de la "
                    "cerradura antes de repetir; no se ha reintentado"
                ) from None
            except asyncio.CancelledError:
                raise
            except Exception:
                raise HomeAssistantError(
                    "Falló la comunicación de paso libre. Comprueba físicamente "
                    "la cerradura antes de repetir; no se ha reintentado"
                ) from None
            finally:
                if not confirmation.done():
                    confirmation.cancel()
                self._confirmation = None
                self._desired_state = None
