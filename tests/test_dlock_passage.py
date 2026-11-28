"""DLOCK-only DP33 polarity, real-report confirmation and state restoration."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.tuya_ble import switch
from custom_components.tuya_ble.devices import TuyaBLEEntity
from custom_components.tuya_ble.dlock_passage import DLOCKPassageModeSwitch
from custom_components.tuya_ble.tuya_ble import fd50_commands
from custom_components.tuya_ble.tuya_ble.const import TuyaBLECode, TuyaBLEDataPointType as DP

MODULE = "custom_components.tuya_ble.dlock_passage"


def entity_for(product="99gv5nmz"):
    entity = DLOCKPassageModeSwitch.__new__(DLOCKPassageModeSwitch)
    entity._device = SimpleNamespace(product_id=product, connected=True,
                                     _client=object(), reconnect=AsyncMock())
    entity._attr_is_on = None
    entity._confirmation = None
    entity._desired_state = None
    entity.async_write_ha_state = MagicMock()
    return entity


def report(entity, value, kind=DP.DT_BOOL, dp_id=33):
    entity._handle_updates([SimpleNamespace(id=dp_id, type=kind, value=value)])


@pytest.mark.parametrize("desired", [True, False])
def test_single_inverted_v4_dp33_and_real_confirmation(desired):
    entity = entity_for()
    async def send(device, dp, kind, value):
        assert entity.is_on is None  # No optimistic state.
        report(entity, not desired)
    with patch(f"{MODULE}.write_dp", AsyncMock(side_effect=send)) as sender:
        asyncio.run(entity.async_turn_on() if desired else entity.async_turn_off())
    sender.assert_awaited_once_with(entity._device, 33, DP.DT_BOOL, bytes((int(not desired),)))
    assert entity.is_on is desired
    assert entity._confirmation is None
    assert entity._desired_state is None
    assert not entity._device._dlock_command_lock.locked()


@pytest.mark.parametrize("value", [False, True])
def test_wire_format_matches_captured_boolean_write(value):
    device = entity_for()._device
    with patch.object(fd50_commands, "send_packet", new_callable=AsyncMock) as send:
        asyncio.run(fd50_commands.write_dp(device, 33, DP.DT_BOOL, bytes((int(value),))))
    send.assert_awaited_once_with(
        device, TuyaBLECode.FUN_SENDER_DPS_V4,
        bytes.fromhex("000000000121010001") + bytes((int(value),)),
        0, True, expected_client=device._client,
    )


@pytest.mark.parametrize("kind,value", [(DP.DT_ENUM, b"\x00"), (DP.DT_BOOL, b"\x02"), (DP.DT_BOOL, b""), (DP.DT_BOOL, b"\x00\x01")])
def test_dp33_whitelist_rejects_wrong_type_or_value(kind, value):
    with pytest.raises(ValueError):
        asyncio.run(fd50_commands.write_dp(entity_for()._device, 33, kind, value))


@pytest.mark.parametrize("bad_reply", ["none", "wrong_type", "wrong_dp", "integer", "opposite"])
def test_ack_or_unrelated_report_does_not_confirm(bad_reply):
    entity = entity_for()
    entity._attr_is_on = False
    async def send(*args):
        if bad_reply == "wrong_type":
            report(entity, False, DP.DT_ENUM)
        elif bad_reply == "wrong_dp":
            report(entity, False, dp_id=47)
        elif bad_reply == "integer":
            report(entity, 0)
        elif bad_reply == "opposite":
            report(entity, True)
    with (
        patch(f"{MODULE}.write_dp", AsyncMock(side_effect=send)) as sender,
        patch(f"{MODULE}.CONFIRM_TIMEOUT", 0.001),
    ):
        with pytest.raises(HomeAssistantError, match="No se confirmó DP33"):
            asyncio.run(entity.async_turn_on())
    sender.assert_awaited_once()
    assert entity.is_on is False
    assert entity._confirmation is None
    assert not entity._device._dlock_command_lock.locked()


@pytest.mark.parametrize("value", [None, False, True])
def test_restores_only_saved_report_not_transport_cache(value):
    entity = entity_for()
    entity._device._cfm_lock_state = {33: value}
    entity._device.datapoints = {33: SimpleNamespace(type=DP.DT_BOOL, value=True)}
    entity._device.register_callback = MagicMock(return_value=lambda: None)
    entity.async_on_remove = MagicMock()
    with patch.object(TuyaBLEEntity, "async_added_to_hass", new_callable=AsyncMock):
        asyncio.run(entity.async_added_to_hass())
    assert entity.is_on is (None if value is None else not value)
    entity._handle_coordinator_update()
    assert entity.is_on is (None if value is None else not value)


def test_connect_settle_then_send_without_status_query():
    entity = entity_for()
    entity._device.connected = False
    order = []
    async def connect():
        order.append("connect")
        entity._device.connected = True
    async def settle(delay):
        assert delay == 0.5
        order.append("settle")
    async def send(*args):
        order.append("send")
        report(entity, False)
    entity._device.reconnect.side_effect = connect
    with patch(f"{MODULE}.asyncio.sleep", side_effect=settle), patch(f"{MODULE}.write_dp", AsyncMock(side_effect=send)):
        asyncio.run(entity.async_turn_on())
    assert order == ["connect", "settle", "send"]


@pytest.mark.parametrize("error", [RuntimeError("secret bytes"), asyncio.CancelledError()])
def test_failure_cleanup_and_no_retry(error):
    entity = entity_for()
    with patch(f"{MODULE}.write_dp", AsyncMock(side_effect=error)) as send:
        with pytest.raises((HomeAssistantError, asyncio.CancelledError)) as caught:
            asyncio.run(entity.async_turn_off())
    assert "secret bytes" not in str(caught.value)
    send.assert_awaited_once()
    assert entity._confirmation is None
    assert not entity._device._dlock_command_lock.locked()


@pytest.mark.parametrize("product", ["b3aouluh", "okkyfgfs", "7a4xvbtt", "8gza4o8a"])
def test_dlock_control_refuses_legacy_products(product):
    entity = entity_for(product)
    with patch(f"{MODULE}.write_dp", new_callable=AsyncMock) as send:
        with pytest.raises(HomeAssistantError):
            asyncio.run(entity.async_turn_on())
    send.assert_not_awaited()
    entity._device.reconnect.assert_not_awaited()


@pytest.mark.parametrize("product", ["99gv5nmz", "b3aouluh", "okkyfgfs", "7a4xvbtt", "8gza4o8a"])
def test_switch_setup_routes_only_dlock_to_new_control(product):
    hass = MagicMock()
    hass.data = {"tuya_ble": {"entry": SimpleNamespace(device=SimpleNamespace(product_id=product, device_id="test-unit"))}}
    added = MagicMock()
    with (
        patch(f"{MODULE}.DLOCKPassageModeSwitch") as dlock,
        patch.object(switch, "TuyaBLEPassageModeSwitch") as legacy,
        patch.object(switch.er, "async_get") as registry,
    ):
        registry.return_value.async_get_entity_id.return_value = None
        asyncio.run(switch.async_setup_entry(hass, SimpleNamespace(entry_id="entry"), added))
    assert dlock.call_count == (product == "99gv5nmz")
    assert legacy.call_count == (product in switch.PASSAGE_PRODUCTS)
    if product == "99gv5nmz":
        registry.assert_not_called()
