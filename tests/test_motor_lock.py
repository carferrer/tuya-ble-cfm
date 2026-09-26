"""Check motor-state mapping and that both lock actions use the tested DP6 path."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


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


def _lock():
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
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(
        compile(module, str(ROOT / "lock.py"), "exec", flags=__import__("__future__").annotations.compiler_flag),
        namespace,
    )
    dp_cache = {47: None}
    device = SimpleNamespace(
        datapoints=dp_cache,
        register_callback=lambda callback: lambda: None,
    )
    data = SimpleNamespace(coordinator=None, device=device, product=None)
    entity = namespace["TuyaBLEMotorLock"](None, data)
    return entity, device, dp_cache, commands


def test_motor_reports_set_lock_state_without_command_guessing():
    entity, device, cache, commands = _lock()
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


def test_initial_state_uses_cached_motor_report_only():
    entity, _, cache, _ = _lock()
    cache[47] = SimpleNamespace(id=47, type="bool", value=False)
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_is_locked is True
