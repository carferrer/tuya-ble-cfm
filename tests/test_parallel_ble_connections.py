"""A stalled BLE proxy must not block a different lock's connection."""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "custom_components/tuya_ble/tuya_ble/tuya_ble.py"
)


class FakeClient:
    is_connected = True

    async def start_notify(self, _characteristic, _callback):
        pass


class FakeDevice:
    def __init__(self, address: str):
        self.address = address
        self.rssi = -70
        self._ble_device = SimpleNamespace(address=address)
        self._client = None
        self._is_paired = False
        self._expected_disconnect = False
        self._connect_lock = asyncio.Lock()

    def _disconnected(self, _client):
        pass

    def _notification_handler(self, _sender, _data):
        pass

    def _build_pairing_request(self):
        return b""

    async def _send_packet_while_connected(self, *_args):
        return True

    def _fire_connected_callbacks(self):
        pass

    def _fire_connection_status_callbacks(self):
        pass


def _load_connection_method(establish_connection):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    device_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TuyaBLEDevice"
    )
    method = next(
        node for node in device_class.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_ensure_connected"
    )
    method.decorator_list = []
    namespace = {
        "asyncio": asyncio,
        "_LOGGER": logging.getLogger(__name__),
        "BleakNotFoundError": RuntimeError,
        "BLEAK_EXCEPTIONS": (RuntimeError,),
        "BleakClientWithServiceCache": FakeClient,
        "CHARACTERISTIC_NOTIFY": object(),
        "TuyaBLECode": SimpleNamespace(
            FUN_SENDER_DEVICE_INFO=1, FUN_SENDER_PAIR=2
        ),
        "establish_connection": establish_connection,
        # Makes this test fail against the old globally serialized method.
        "global_connect_lock": asyncio.Lock(),
    }
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    FakeDevice._ensure_connected = namespace["_ensure_connected"]


def test_separate_locks_connect_in_parallel():
    async def run():
        devices = {address: FakeDevice(address) for address in ("LOCK-A", "LOCK-B")}
        started = []
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def connect(_client_type, _ble_device, address, _disconnect, **_kwargs):
            started.append(address)
            if len(started) == 2:
                both_started.set()
            await release.wait()
            devices[address]._is_paired = True
            return FakeClient()

        _load_connection_method(connect)
        tasks = [asyncio.create_task(device._ensure_connected()) for device in devices.values()]
        try:
            await asyncio.wait_for(both_started.wait(), timeout=2)
        finally:
            release.set()
            await asyncio.gather(*tasks)

        assert set(started) == set(devices)
        assert all(device._client is not None for device in devices.values())

    asyncio.run(run())


def test_same_lock_still_uses_one_connection_attempt():
    async def run():
        device = FakeDevice("LOCK-A")
        started = 0
        first_started = asyncio.Event()
        release = asyncio.Event()

        async def connect(_client_type, _ble_device, _address, _disconnect, **_kwargs):
            nonlocal started
            started += 1
            first_started.set()
            await release.wait()
            device._is_paired = True
            return FakeClient()

        _load_connection_method(connect)
        tasks = [asyncio.create_task(device._ensure_connected()) for _ in range(2)]
        try:
            await asyncio.wait_for(first_started.wait(), timeout=2)
            await asyncio.sleep(0.05)
            assert started == 1
        finally:
            release.set()
            await asyncio.gather(*tasks)

        assert started == 1

    asyncio.run(run())
