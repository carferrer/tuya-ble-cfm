"""Check that an offline DP71 seed survives restart and can be invalidated."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble"
spec = importlib.util.spec_from_file_location("la_t01_unlock", ROOT / "la_t01_unlock.py")
unlock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(unlock)


def _session_class():
    tree = ast.parse((ROOT / "la_t01_session.py").read_text(encoding="utf-8"))
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LA_T01OfflineSession"
    )

    class FakeStore:
        def __init__(self, hass, version, key):
            self._storage = hass.storage
            self._key = key

        async def async_load(self):
            return self._storage.get(self._key)

        async def async_save(self, data):
            self._storage[self._key] = data

    namespace = {
        "Store": FakeStore,
        "STORE_VERSION": 1,
        "asyncio": asyncio,
        "time": SimpleNamespace(time=lambda: 1800000100),
        "dp71_seed_from_command": unlock.dp71_seed_from_command,
        "dp71_command_from_seed": unlock.dp71_command_from_seed,
    }
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(compile(module, "la_t01_session.py", "exec"), namespace)
    return namespace["LA_T01OfflineSession"]


def test_seed_survives_restart_and_failed_pairing_clears_disk():
    session_type = _session_class()
    tasks = []
    hass = SimpleNamespace(
        storage={},
        async_create_task=lambda coro, name: tasks.append(asyncio.create_task(coro)),
    )
    command = (
        b"\xff\xff\x00\x05ABCDEFGH\x01"
        + (1800000000).to_bytes(4, "big")
        + b"\x00\x01"
    )

    async def run():
        first_device = SimpleNamespace(device_id="lock-1", _cfm_dp71_local_command=None)
        first = session_type(hass, "entry-1", first_device)
        await first.async_load()
        assert first_device._cfm_dp71_local_command is None

        await first.async_save_command(command)
        assert first_device._cfm_dp71_local_persisted is True
        assert len(bytes.fromhex(hass.storage[first._store._key]["seed"])) == 11
        assert "device_id" in hass.storage[first._store._key]

        restarted_device = SimpleNamespace(device_id="lock-1", _cfm_dp71_local_command=None)
        restarted = session_type(hass, "entry-1", restarted_device)
        await restarted.async_load()
        restored = restarted_device._cfm_dp71_local_command
        assert restarted_device._cfm_dp71_local_persisted is True
        assert restored[:13] == command[:13]
        assert restored[13:17] == (1800000100).to_bytes(4, "big")

        different_device = SimpleNamespace(device_id="lock-2", _cfm_dp71_local_command=None)
        await session_type(hass, "entry-1", different_device).async_load()
        assert different_device._cfm_dp71_local_command is None

        restarted.invalidate()
        assert restarted_device._cfm_dp71_local_command is None
        await asyncio.gather(*tasks)
        assert hass.storage[restarted._store._key] == {}

    asyncio.run(run())
