"""CFM power-saving connection policy for Tuya BLE locks.

This keeps the Tuya BLE device in its normal connection mode internally, but
opens the GATT link only when needed and drops it after a short idle period.
It intentionally mirrors the first CFM lock power-saver implementation that
was validated on real lock hardware.
"""

from __future__ import annotations

import asyncio
import logging
from types import MethodType
from typing import Any

_LOGGER = logging.getLogger(__name__)

LOCK_POWER_SAVER_CATEGORIES = {"ms", "jtmspro"}
DEFAULT_LOCK_IDLE_DISCONNECT_DELAY = 30


def enable_lock_power_saver(
    device: Any,
    idle_disconnect_delay: int = DEFAULT_LOCK_IDLE_DISCONNECT_DELAY,
) -> bool:
    """Enable the tested CFM on-demand policy for supported lock categories."""
    if getattr(device, "category", None) not in LOCK_POWER_SAVER_CATEGORIES:
        return False

    if getattr(device, "_lock_power_saver_enabled", False):
        return True

    device._lock_power_saver_enabled = True
    device._lock_power_saver_idle_disconnect_delay = max(
        5, int(idle_disconnect_delay)
    )
    device._lock_power_saver_idle_task = None
    device._lock_power_saver_idle_disconnecting = False
    device._lock_power_saver_stopped = False
    device._lock_power_saver_reachable = False

    original_ensure_connected = device._ensure_connected
    original_send_packet = device._send_packet
    original_send_response = device._send_response
    original_resend_packets = device._resend_packets
    original_reconnect = device._reconnect
    original_execute_disconnect = device._execute_disconnect
    original_fire_disconnected_callbacks = device._fire_disconnected_callbacks
    original_stop = device.stop

    async def _idle_disconnect(self: Any) -> None:
        """Disconnect the BLE link after the configured idle period."""
        try:
            await asyncio.sleep(self._lock_power_saver_idle_disconnect_delay)

            while self._operation_lock.locked() or self._input_expected_responses:
                await asyncio.sleep(0.25)

            if self._lock_power_saver_stopped:
                return

            if not (self._client and self._client.is_connected):
                return

            _LOGGER.debug(
                "%s: CFM lock idle for %ss, disconnecting to save battery",
                self.address,
                self._lock_power_saver_idle_disconnect_delay,
            )

            self._lock_power_saver_idle_disconnecting = True
            try:
                await asyncio.wait_for(original_execute_disconnect(), 10)
            except TimeoutError:
                _LOGGER.debug("%s: CFM lock idle disconnect timed out", self.address)
            finally:
                await asyncio.sleep(0.5)
                self._expected_disconnect = False
                self._is_paired = False
                self._lock_power_saver_idle_disconnecting = False

        except asyncio.CancelledError:
            pass

    def _touch(self: Any) -> None:
        """Restart the CFM idle timer after BLE activity."""
        if self._lock_power_saver_stopped:
            return

        task = self._lock_power_saver_idle_task
        if task and not task.done():
            task.cancel()

        self._lock_power_saver_idle_task = asyncio.create_task(
            _idle_disconnect(self)
        )

    async def _ensure_connected(self: Any) -> None:
        """Reconnect on demand after an intentional CFM idle disconnect."""
        if self._lock_power_saver_stopped:
            return

        while self._lock_power_saver_idle_disconnecting:
            await asyncio.sleep(0.05)

        self._expected_disconnect = False
        await original_ensure_connected()

        if self._client and self._client.is_connected and self._is_paired:
            self._lock_power_saver_reachable = True
            _touch(self)

    async def _send_packet(
        self: Any,
        code: Any,
        data: bytes,
        wait_for_response: bool = True,
    ) -> None:
        await original_send_packet(code, data, wait_for_response)
        _touch(self)

    async def _send_response(
        self: Any,
        code: Any,
        data: bytes,
        response_to: int,
    ) -> None:
        await original_send_response(code, data, response_to)
        _touch(self)

    async def _resend_packets(self: Any, packets: list[bytes]) -> None:
        await original_resend_packets(packets)
        _touch(self)

    async def _reconnect(self: Any, delay: float = 0) -> None:
        """Avoid reconnecting an idle lock unless active traffic needs it."""
        if self._lock_power_saver_stopped or self._lock_power_saver_idle_disconnecting:
            return

        if not (self._operation_lock.locked() or self._input_expected_responses):
            _LOGGER.debug(
                "%s: CFM lock disconnected while idle; automatic reconnect suppressed",
                self.address,
            )
            return

        await original_reconnect(delay)

    def _fire_disconnected_callbacks(self: Any) -> None:
        """Keep HA entities available across intentional idle disconnects."""
        if self._lock_power_saver_idle_disconnecting:
            _LOGGER.debug(
                "%s: Suppressing HA disconnect callbacks for CFM lock idle disconnect",
                self.address,
            )
            return

        original_fire_disconnected_callbacks()

    async def _stop(self: Any) -> None:
        """Cancel the CFM idle timer and perform the normal permanent stop."""
        self._lock_power_saver_stopped = True
        task = self._lock_power_saver_idle_task
        if task and not task.done():
            task.cancel()
        self._lock_power_saver_idle_task = None
        await original_stop()

    device._lock_power_saver_touch = MethodType(_touch, device)
    device._ensure_connected = MethodType(_ensure_connected, device)
    device._send_packet = MethodType(_send_packet, device)
    device._send_response = MethodType(_send_response, device)
    device._resend_packets = MethodType(_resend_packets, device)
    device._reconnect = MethodType(_reconnect, device)
    device._fire_disconnected_callbacks = MethodType(
        _fire_disconnected_callbacks, device
    )
    device.stop = MethodType(_stop, device)

    _LOGGER.info(
        "%s: Enabled tested CFM lock power saver with %ss idle disconnect",
        device.address,
        device._lock_power_saver_idle_disconnect_delay,
    )
    return True
