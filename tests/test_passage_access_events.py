"""Passage activation events for all implemented families, without control writes."""

import asyncio
from types import SimpleNamespace

import pytest

from test_alarm_events import DPType, FakeEvent, FakeStore, load_code

FAMILIES = [
    ("b3aouluh", True),
    ("7a4xvbtt", False),
    ("8gza4o8a", False),
    ("99gv5nmz", False),
]


def make_event(product, initial=None, unit="unit"):
    namespace = {
        "DOMAIN": "tuya_ble",
        "PRODUCT_B3AOULUH": "b3aouluh",
        "PRODUCT_7A4XVBTT": "7a4xvbtt",
        "PRODUCT_8GZA4O8A": "8gza4o8a",
        "PRODUCT_99GV5NMZ": "99gv5nmz",
        "TuyaBLEDataPointType": DPType,
        "EventEntity": FakeEvent,
        "Store": FakeStore,
        "callback": lambda fn: fn,
        "get_device_info": lambda device: {},
    }
    load_code("access.py", namespace)
    load_code("event.py", namespace)
    listeners = []
    device = SimpleNamespace(
        product_id=product, device_id=unit,
        datapoints={33: None if initial is None else dp(initial)},
        _cfm_received_dp_events=[],
        register_callback=lambda cb: (listeners.append(cb), lambda: listeners.remove(cb))[1],
    )
    return namespace["TuyaBLEAccessEvent"]({}, SimpleNamespace(entry_id=unit), device)


def dp(value, kind=DPType.DT_BOOL, dp_id=33):
    return SimpleNamespace(id=dp_id, type=kind, value=value, timestamp=1800000000)


@pytest.mark.parametrize("product,enabled", FAMILIES)
def test_confirmed_activation_emits_once_and_deactivation_is_silent(product, enabled):
    entity = make_event(product, not enabled)
    asyncio.run(entity.async_added_to_hass())
    assert "passage_mode_enabled" in entity._attr_event_types
    entity._handle_updates([dp(enabled), dp(enabled), dp(not enabled)])
    assert len(entity.emitted) == 1
    kind, payload = entity.emitted[0]
    assert kind == "passage_mode_enabled"
    assert payload == {
        "event_type_id": 33, "method": "passage_mode", "dp_id": 33,
        "event_time": payload["received_at"],
        "received_at": payload["received_at"], "recovered": False,
    }
    entity._handle_updates([dp(enabled)])
    assert len(entity.emitted) == 2


@pytest.mark.parametrize("product,enabled", FAMILIES)
def test_startup_and_reload_already_enabled_never_emit(product, enabled):
    for _ in range(2):
        entity = make_event(product, enabled)
        asyncio.run(entity.async_added_to_hass())
        entity._handle_updates([dp(enabled), dp(enabled)])
        assert entity._passage_mode is True
        assert not entity.emitted


@pytest.mark.parametrize("product,enabled", FAMILIES)
def test_unknown_initial_state_is_only_baseline(product, enabled):
    entity = make_event(product)
    asyncio.run(entity.async_added_to_hass())
    entity._handle_updates([dp(enabled), dp(enabled), dp(not enabled)])
    assert not entity.emitted
    entity._handle_updates([dp(enabled)])
    assert len(entity.emitted) == 1


@pytest.mark.parametrize("product,enabled", FAMILIES)
def test_outgoing_cache_changes_do_not_emit_without_device_report(product, enabled):
    entity = make_event(product, not enabled)
    asyncio.run(entity.async_added_to_hass())
    entity._device.datapoints[33].value = enabled
    assert not entity.emitted
    assert entity._passage_mode is False
    entity._handle_updates([dp(enabled)])
    assert len(entity.emitted) == 1


@pytest.mark.parametrize("product,enabled", FAMILIES)
def test_invalid_dp33_and_alarm_cannot_emit_passage_event(product, enabled):
    entity = make_event(product, not enabled)
    asyncio.run(entity.async_added_to_hass())
    entity._handle_updates([
        dp(int(enabled)), dp(str(enabled)), dp(None),
        dp(enabled, DPType.DT_ENUM), dp(0, DPType.DT_ENUM, 21),
    ])
    assert not entity.emitted
    assert entity._passage_mode is False
    entity._handle_updates([dp(enabled)])
    assert len(entity.emitted) == 1


@pytest.mark.parametrize("product", ["okkyfgfs", "unsupported"])
def test_unimplemented_products_do_not_gain_passage_events(product):
    entity = make_event(product, False)
    asyncio.run(entity.async_added_to_hass())
    entity._handle_updates([dp(True), dp(False), dp(True)])
    assert "passage_mode_enabled" not in entity._attr_event_types
    assert not entity.emitted


@pytest.mark.parametrize("product,enabled", FAMILIES)
def test_events_do_not_cross_between_same_product_units(product, enabled):
    units = [make_event(product, not enabled, f"unit-{n}") for n in range(3)]
    for entity in units:
        asyncio.run(entity.async_added_to_hass())
    units[1]._handle_updates([dp(enabled)])
    assert [len(entity.emitted) for entity in units] == [0, 1, 0]
    assert len({entity._attr_unique_id for entity in units}) == 3


@pytest.mark.parametrize("product,enabled", FAMILIES)
def test_confirmed_transition_during_async_store_load_is_emitted_once(product, enabled):
    entity = make_event(product, not enabled)
    entity._store.on_load = lambda: entity._handle_updates([dp(enabled), dp(enabled)])
    asyncio.run(entity.async_added_to_hass())
    assert len(entity.emitted) == 1
    entity._handle_updates([dp(enabled)])
    assert len(entity.emitted) == 1
