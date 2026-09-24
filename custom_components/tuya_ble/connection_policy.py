"""Per-device BLE connection policies for supported Tuya locks."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .tuya_ble import TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)

CONF_CONNECTION_MODE = "connection_mode"
CONF_SYNC_INTERVAL = "sync_interval_minutes"

CONNECTION_MODE_POWER_SAVE = "power_save"
CONNECTION_MODE_PERIODIC_SYNC = "periodic_sync"
CONNECTION_MODE_KEEP_ALIVE = "keep_alive"
CONNECTION_MODES = {
    CONNECTION_MODE_POWER_SAVE: "Battery saver",
    CONNECTION_MODE_PERIODIC_SYNC: "Battery saver + periodic sync",
    CONNECTION_MODE_KEEP_ALIVE: "Always connected (keep alive)",
}

SYNC_INTERVALS = {
    1: "1 minute",
    2: "2 minutes",
    5: "5 minutes",
    10: "10 minutes",
    15: "15 minutes",
    30: "30 minutes",
}
DEFAULT_CONNECTION_MODE = CONNECTION_MODE_POWER_SAVE
DEFAULT_SYNC_INTERVAL = 5
KEEP_ALIVE_WATCHDOG_SECONDS = 30


def connection_mode(entry: ConfigEntry) -> str:
    """Return configured BLE connection mode."""
    mode = entry.options.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
    if mode not in CONNECTION_MODES:
        return DEFAULT_CONNECTION_MODE
    return str(mode)


def sync_interval_minutes(entry: ConfigEntry) -> int:
    """Return validated periodic synchronization interval."""
    try:
        interval = int(entry.options.get(CONF_SYNC_INTERVAL, DEFAULT_SYNC_INTERVAL))
    except (TypeError, ValueError):
        return DEFAULT_SYNC_INTERVAL
    if interval not in SYNC_INTERVALS:
        return DEFAULT_SYNC_INTERVAL
    return interval


def apply_connection_mode(device: TuyaBLEDevice, mode: str) -> None:
    """Apply a connection mode to the already-enabled power saver wrapper."""
    device._lock_connection_mode = mode

    # The advertising cadence detector is intentionally only active in the
    # pure battery-saver mode. In periodic-sync mode the configured interval is
    # authoritative; otherwise idle b3 advertising bursts can create extra
    # reconnects between scheduled synchronizations. Keep-alive has its own
    # reconnect watchdog and also does not need the advertising detector.
    activity_detection_enabled = mode == CONNECTION_MODE_POWER_SAVE
    device._cfm_activity_detection_enabled = activity_detection_enabled
    device._cfm_activity_armed = False
    device._cfm_activity_fast_streak = 0
    device._cfm_activity_update_in_progress = not activity_detection_enabled

    idle_task = getattr(device, "_lock_power_saver_idle_task", None)
    if mode == CONNECTION_MODE_KEEP_ALIVE and idle_task and not idle_task.done():
        idle_task.cancel()
        device._lock_power_saver_idle_task = None


def setup_connection_policy(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: TuyaBLEDevice,
):
    """Apply this entry's mode and optionally schedule fallback synchronization."""
    mode = connection_mode(entry)
    apply_connection_mode(device, mode)
    device._cfm_connection_mode = mode
    device._cfm_periodic_sync_interval_minutes = sync_interval_minutes(entry)
    device._cfm_periodic_sync_attempt_count = 0
    device._cfm_periodic_sync_success_count = 0
    device._cfm_periodic_sync_last_started_at = None
    device._cfm_periodic_sync_last_error = None
    device._cfm_keep_alive_attempt_count = 0
    device._cfm_keep_alive_success_count = 0
    device._cfm_keep_alive_last_error = None

    if mode == CONNECTION_MODE_KEEP_ALIVE:
        async def _keep_alive(_now) -> None:
            if device.connected:
                return
            device._cfm_keep_alive_attempt_count += 1
            device._cfm_keep_alive_last_error = None
            try:
                _LOGGER.debug(
                    "%s: keep-alive watchdog reconnecting BLE",
                    device.address,
                )
                await device.reconnect()
                await device.update()
                device._cfm_keep_alive_success_count += 1
            except Exception as err:  # noqa: BLE001 - watchdog must survive
                device._cfm_keep_alive_last_error = f"{type(err).__name__}: {err}"
                _LOGGER.exception(
                    "%s: keep-alive BLE reconnect failed",
                    device.address,
                )

        _LOGGER.info("%s: BLE connection mode is keep-alive", device.address)
        return async_track_time_interval(
            hass,
            _keep_alive,
            timedelta(seconds=KEEP_ALIVE_WATCHDOG_SECONDS),
        )

    if mode != CONNECTION_MODE_PERIODIC_SYNC:
        _LOGGER.info("%s: BLE connection mode is battery saver", device.address)
        return None

    interval = sync_interval_minutes(entry)

    async def _periodic_sync(_now) -> None:
        if device.connected:
            return

        device._cfm_periodic_sync_attempt_count += 1
        device._cfm_periodic_sync_last_started_at = time.time()
        device._cfm_periodic_sync_last_error = None
        try:
            _LOGGER.debug(
                "%s: periodic BLE record synchronization starting",
                device.address,
            )
            await device.reconnect()
            await asyncio.sleep(0.1)
            await device.update()
            await asyncio.sleep(0.5)
            await device.update()
            device._cfm_periodic_sync_success_count += 1

            # Keep the GATT window open after the refresh. On b3 locks, cached
            # access records can arrive a few seconds after the DP69 exchange.
            touch = getattr(device, "_lock_power_saver_touch", None)
            if touch is not None:
                touch(5.0)
        except Exception as err:  # noqa: BLE001 - periodic fallback must survive
            device._cfm_periodic_sync_last_error = f"{type(err).__name__}: {err}"
            _LOGGER.exception(
                "%s: periodic BLE record synchronization failed",
                device.address,
            )

    _LOGGER.info(
        "%s: BLE connection mode is periodic sync every %s minutes",
        device.address,
        interval,
    )
    return async_track_time_interval(
        hass,
        _periodic_sync,
        timedelta(minutes=interval),
    )
