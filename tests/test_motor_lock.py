"""Check motor-state mapping and that both lock actions use the tested DP6 path."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble"


class FakeBase:
    def __init__(self, hass, coordinator, device, product, description, domain):
        self._device = device
        self.domain = domain
        self.description = description

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, unsubscribe):
        pass

    def async_write_ha_state(self):
        pass


def _lock(product="b3aouluh"):
    source = ast.parse((ROOT / "lock.py").read_text(encoding="utf-8"))
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "TuyaBLEMotorLock")
    cls.bases = [ast.Name(id="FakeBase", ctx=ast.Load())]
    commands = []

    async def send_unlock(device):
        commands.append(device)

    namespace = {
        "FakeBase": FakeBase,
        "callback": lambda func: func,
        "LockEntityDescription": lambda **kwargs: SimpleNamespace(**kwargs),
        "TuyaBLEDataPointType": SimpleNamespace(DT_BOOL="bool"),
        "DP_MOTOR_STATE": 47,
        "async_press_bluetooth_unlock": send_unlock,
    }
    setup = next(
        node for node in source.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    namespace.update(
        DOMAIN="tuya_ble", PRODUCT_B3AOULUH="b3aouluh", PRODUCT_OKKYFGFS="okkyfgfs"
    )
    module = ast.fix_missing_locations(ast.Module(body=[cls, setup], type_ignores=[]))
    exec(
        compile(module, str(ROOT / "lock.py"), "exec", flags=__import__("__future__").annotations.compiler_flag),
        namespace,
    )
    dp_cache = {47: None}
    device = SimpleNamespace(
        product_id=product,
        datapoints=dp_cache,
        register_callback=lambda callback: lambda: None,
    )
    data = SimpleNamespace(coordinator=None, device=device, product=None)
    hass = SimpleNamespace(data={"tuya_ble": {"entry": data}})
    entities = []
    asyncio.run(namespace["async_setup_entry"](
        hass, SimpleNamespace(entry_id="entry"), entities.extend
    ))
    if product in ("b3aouluh", "okkyfgfs"):
        assert len(entities) == 1
    else:
        assert not entities
    return entities[0] if entities else None, device, dp_cache, commands


@pytest.mark.parametrize("product", ["b3aouluh", "okkyfgfs"])
def test_motor_reports_set_lock_state_without_command_guessing(product):
    entity, device, cache, commands = _lock(product)
    assert entity.domain == "lock"
    assert entity.available is True
    assert entity._attr_is_locked is None
    entity._handle_updates([SimpleNamespace(id=47, type="bool", value=False)])
    assert entity._attr_is_locked is True
    entity._handle_updates([SimpleNamespace(id=47, type="bool", value=True)])
    assert entity._attr_is_locked is False
    entity._handle_updates([SimpleNamespace(id=47, type="raw", value=False)])
    assert entity._attr_is_locked is False
    asyncio.run(entity.async_lock())
    asyncio.run(entity.async_unlock())
    assert commands == [device, device]
    assert entity._attr_is_locked is False


@pytest.mark.parametrize("product", ["b3aouluh", "okkyfgfs"])
def test_initial_state_uses_cached_motor_report_only(product):
    entity, _, cache, _ = _lock(product)
    cache[47] = SimpleNamespace(id=47, type="bool", value=False)
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_is_locked is True


@pytest.mark.parametrize("product", ["b3aouluh", "okkyfgfs"])
def test_restored_state_wins_over_provisional_transport_cache(product):
    entity, device, cache, _ = _lock(product)
    cache[47] = SimpleNamespace(id=47, type="bool", value=True)
    device._cfm_lock_state = SimpleNamespace(get=lambda dp_id: False)
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_is_locked is True


def test_unsupported_product_has_no_motor_lock_entity():
    entity, _, _, _ = _lock("unsupported")
    assert entity is None
