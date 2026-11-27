"""DLOCK state evidence, replay protection and isolated local command routing."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.tuya_ble.dlock_attempts import notify_local_open
from custom_components.tuya_ble.dlock_lock import DLOCKLock
from custom_components.tuya_ble.tuya_ble import TuyaBLEDataPointType as DP

MODULE = "custom_components.tuya_ble.dlock_lock"


def entity_for(unit=1):
    device = SimpleNamespace(
        product_id="99gv5nmz", device_id=f"unit-{unit}",
        register_callback=MagicMock(return_value=MagicMock()),
    )
    data = SimpleNamespace(device=device, coordinator=None, product=None)
    def init(self, hass, coordinator, device, product, description, domain):
        self._device = device
        self._attr_unique_id = f"{device.device_id}-{description.key}"
    with patch(f"{MODULE}.TuyaBLEEntity.__init__", init):
        entity = DLOCKLock(None, data, SimpleNamespace(options={}))
    entity.async_write_ha_state = MagicMock()
    entity.async_on_remove = MagicMock()
    return entity


def report(dp, value, timestamp, kind=None):
    return SimpleNamespace(
        id=dp, value=value, timestamp=timestamp,
        type=kind or (DP.DT_BOOL if dp in (33, 47) else DP.DT_VALUE),
    )


@pytest.mark.parametrize("dp", [12, 13, 14, 15, 19, 55, 62, 63])
def test_live_access_opens_then_motor_confirms_close(dp):
    entity = entity_for()
    now = datetime.now(UTC).timestamp()
    entity._handle_updates([report(dp, 100, now - 0.5)])
    assert entity.is_locked is False
    assert entity.extra_state_attributes["state_source"] == "access_event"
    assert entity.extra_state_attributes["state_estimated"] is True
    entity._handle_updates([report(47, False, now)])
    assert entity.is_locked is True
    assert entity.extra_state_attributes["state_source"] == "motor"
    assert entity.extra_state_attributes["state_estimated"] is False
    # Repeated/out-of-order records may not reopen an already confirmed lock.
    entity._handle_updates([report(dp, 100, now - 0.5)])
    assert entity.is_locked is True


@pytest.mark.parametrize("offset", [-3600, -10, 10])
def test_old_and_future_records_do_not_change_current_state(offset):
    entity = entity_for()
    now = datetime.now(UTC).timestamp()
    entity._handle_updates([report(47, False, now)])
    for dp, value in ((12, 100), (33, False), (47, True)):
        entity._handle_updates([report(dp, value, now + offset)])
    assert entity.is_locked is True
    assert entity.extra_state_attributes["passage_mode"] is None


def test_passage_requires_opening_and_disabling_does_not_fake_closing():
    entity = entity_for()
    now = datetime.now(UTC).timestamp()
    entity._handle_updates([report(47, False, now - 1.5), report(33, False, now - 1.4)])
    assert entity.is_locked is True
    entity._handle_updates([report(12, 100, now - 1)])
    assert entity.is_locked is False
    entity._handle_updates([report(47, False, now - 0.8)])
    assert entity.is_locked is False
    assert entity.extra_state_attributes["state_source"] == "passage_mode"
    entity._handle_updates([report(33, True, now - 0.5)])
    assert entity.is_locked is False
    assert entity.extra_state_attributes["state_source"] == "awaiting_motor"
    entity._handle_updates([report(47, False, now - 0.7)])
    assert entity.is_locked is False
    entity._handle_updates([report(47, False, now)])
    assert entity.is_locked is True


def test_invalid_types_and_unknown_datapoints_cannot_open():
    entity = entity_for()
    now = datetime.now(UTC).timestamp()
    entity._handle_updates([
        report(12, 100, now, DP.DT_RAW),
        report(47, 1, now, DP.DT_BOOL),
        report(33, 0, now, DP.DT_BOOL),
        report(21, 0, now, DP.DT_ENUM),
        report(71, b"unmatched", now, DP.DT_RAW),
    ])
    assert entity.is_locked is None


def test_startup_is_unknown_and_listeners_are_per_unit_and_removed():
    async def run():
        units = [entity_for(n) for n in range(3)]
        for entity in units:
            entity._device._cfm_lock_state = SimpleNamespace(get=lambda dp: False)
            with patch(f"{MODULE}.TuyaBLEEntity.async_added_to_hass", new_callable=AsyncMock):
                await entity.async_added_to_hass()
            assert entity.is_locked is None
            assert entity.extra_state_attributes["passage_mode"] is True
        assert len({unit.unique_id for unit in units}) == 3
        notify_local_open(units[1]._device)
        assert [unit.is_locked for unit in units] == [None, False, None]
        assert units[1].extra_state_attributes["state_source"] == "passage_mode"
        for entity in units:
            for call in entity.async_on_remove.call_args_list:
                call.args[0]()
            assert not entity._device._dlock_open_listeners
    asyncio.run(run())


def test_unlock_and_open_use_button_path_but_lock_never_sends_an_opening():
    async def run():
        entity = entity_for()
        with patch(f"{MODULE}.DLOCKUnlockTestButton.async_press", new_callable=AsyncMock) as press:
            await entity.async_unlock()
            await entity.async_open()
            assert press.await_count == 2
            assert all(call.args == (entity,) for call in press.call_args_list)
            with pytest.raises(HomeAssistantError, match="cierre verificada"):
                await entity.async_lock()
            assert press.await_count == 2
        assert entity.is_locked is None
    asyncio.run(run())
