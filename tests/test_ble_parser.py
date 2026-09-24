"""Execute the production parser without importing HA or opening a BLE adapter."""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from struct import pack, unpack
import time
from types import SimpleNamespace

from Crypto.Cipher import AES
import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble"


@pytest.fixture
def device():
    namespace = {
        "AES": AES,
        "pack": pack,
        "unpack": unpack,
        "time": time,
        "_LOGGER": logging.getLogger("parser_test"),
    }
    for name in ("const.py", "exceptions.py"):
        exec(compile((ROOT / "tuya_ble" / name).read_text(), name, "exec"), namespace)
    tree = ast.parse((ROOT / "tuya_ble/tuya_ble.py").read_text())
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "TuyaBLEDevice"
    )
    methods = {
        "_pack_int",
        "_unpack_int",
        "_calc_crc16",
        "_get_key",
        "_clean_input",
        "_parse_input",
        "_notification_handler",
        "_receive_notification",
        "_parse_timestamp",
        "_parse_datapoints_v3",
        "_handle_command_or_response",
    }
    cls.body = [
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in methods
    ]
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            cls,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), "parser", "exec"), namespace)
    obj = namespace["TuyaBLEDevice"]()
    obj.address = "test"
    obj._auth_key = obj._login_key = obj._session_key = bytes(16)
    obj._clean_input()
    obj.received = []
    obj._handle_command_or_response = lambda *args: obj.received.append(args)
    obj.ns = namespace
    return obj


def frame(device, payload=b"\x00", *, length=None, bad_crc=False, flag=1, code=3):
    raw = (
        pack(">IIHH", 1, 0, code, len(payload) if length is None else length) + payload
    )
    raw += pack(">H", device._calc_crc16(raw) ^ int(bad_crc))
    raw += bytes((-len(raw)) % 16)
    return (
        bytes([flag])
        + bytes(16)
        + AES.new(bytes(16), AES.MODE_CBC, bytes(16)).encrypt(raw)
    )


def packets(device, data):
    return [
        device._pack_int(0) + device._pack_int(len(data)) + b"\x30" + data[:16],
        b"\x01" + data[16:],
    ]


def send(device, data):
    for part in packets(device, data):
        device._notification_handler(0, bytearray(part))


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x80",
        b"\x80" * 5,
        b"\x00",
        b"\x00\x00\x30",
        b"\x00\x21",
        b"\x00\x22\x30a",
        b"\x01abc",
    ],
)
def test_bad_fragment_does_not_escape_and_next_frame_recovers(device, data):
    device._notification_handler(0, bytearray(data))
    assert device._input_buffer is None
    send(device, frame(device))
    assert len(device.received) == 1


@pytest.mark.parametrize(
    "options",
    [{"length": 4}, {"length": 3}, {"length": 65535}, {"bad_crc": True}, {"flag": 2}],
)
def test_invalid_encrypted_frame_is_discarded(device, options):
    send(device, frame(device, **options))
    assert not device.received
    assert device._input_buffer is None
    send(device, frame(device))
    assert len(device.received) == 1


def test_new_start_resynchronizes_incomplete_frame(device, caplog):
    parts = packets(device, frame(device))
    with caplog.at_level(logging.DEBUG):
        device._notification_handler(0, parts[0])
        device._notification_handler(0, parts[0])
        device._notification_handler(0, parts[1])
    assert len(device.received) == 1
    assert all(record.levelno < logging.WARNING for record in caplog.records)


def test_missing_or_duplicate_fragment_discards_frame(device, caplog):
    parts = packets(device, frame(device))
    with caplog.at_level(logging.DEBUG):
        device._notification_handler(0, parts[0])
        device._notification_handler(0, b"\x02broken")
        device._notification_handler(0, parts[1])
        send(device, frame(device))
        device._notification_handler(0, parts[1])
    assert len(device.received) == 1
    assert all(record.levelno < logging.WARNING for record in caplog.records)


def test_overflow_and_empty_fragment_reset(device):
    parts = packets(device, frame(device))
    for ending in (parts[1] + b"extra", b"\x01"):
        device._notification_handler(0, parts[0])
        device._notification_handler(0, ending)
        assert device._input_buffer is None
    assert not device.received


@pytest.mark.parametrize("payload", [b"", b"\x01\x00"])
def test_command_length_error_is_contained(device, payload):
    device._handle_command_or_response = type(
        device
    )._handle_command_or_response.__get__(device)
    device._input_expected_responses = {}
    send(device, frame(device, payload))
    assert device._input_buffer is None


@pytest.mark.parametrize(
    "tail", [b"\x0d", b"\x0d\x02", b"\x0d\x02\x04\x00", b"\x0d\x03\x01\xff"]
)
def test_invalid_dp_does_not_publish_partial_access(device, tail):
    updates = []
    device._datapoints = SimpleNamespace(
        _update_from_device=lambda *args: updates.append(args)
    )
    device._fire_callbacks = lambda *args: updates.append(args)
    with pytest.raises(
        (device.ns["TuyaBLEDataLengthError"], device.ns["TuyaBLEDataFormatError"])
    ):
        device._parse_datapoints_v3(123, 0, b"\x0c\x02\x04\x00\x00\x00\x01" + tail, 0)
    assert not updates


def test_valid_dp69_and_access_payload_values_preserved(device):
    class Datapoints(dict):
        def _update_from_device(self, id, timestamp, flags, type, value):
            self[id] = (id, timestamp, flags, type, value)

    device._datapoints = Datapoints()
    received = []
    device._fire_callbacks = received.extend
    device._parse_datapoints_v3(
        123, 0, b"\x45\x00\x03\xff\xff\x01\x0c\x02\x04\x00\x00\x00\x07", 0
    )
    assert [(dp[0], dp[1], dp[4]) for dp in received] == [
        (69, 123, b"\xff\xff\x01"),
        (12, 123, 7),
    ]
