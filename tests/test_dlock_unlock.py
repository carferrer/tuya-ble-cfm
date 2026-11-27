"""Private import validation using fictional data, runnable without HA."""

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "dlock_unlock_under_test",
    Path(__file__).parents[1] / "custom_components/tuya_ble/dlock_unlock.py",
)
unlock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(unlock)
ADDRESS = "AA:BB:CC:DD:EE:01"
SEED = bytes.fromhex("0005616263646566676801")


def valid_import():
    return {"version": 1, "product_id": "99gv5nmz", "device_id": "fictional-unit",
            "address": ADDRESS, "seed": SEED.hex()}


def test_manual_seed_normalizes_and_binds_identity():
    record = unlock.seed_record("  " + SEED.hex().upper() + "\n", "99gv5nmz", "fictional-unit", ADDRESS.lower())
    assert record == valid_import()
    # Simulate persisted options after restarting HA.
    assert unlock.configured_seed(json.loads(json.dumps(record)), "99gv5nmz", "fictional-unit", ADDRESS) == SEED


@pytest.mark.parametrize("value", [None, 123, "", "g" * 22, "00" * 11, "0005" + "11" * 8 + "00"])
def test_manual_seed_rejects_invalid_input_without_echo(value):
    with pytest.raises(ValueError) as err:
        unlock.parse_seed(value)
    if value:
        assert str(value) not in str(err.value)


@pytest.mark.parametrize("field,value", [("device_id", "different"), ("address", "AA:BB:CC:DD:EE:02"), ("product_id", "7a4xvbtt"), ("version", True)])
def test_saved_seed_cannot_cross_device_identity(field, value):
    record = valid_import()
    record[field] = value
    with pytest.raises(ValueError):
        unlock.configured_seed(record, "99gv5nmz", "fictional-unit", ADDRESS)


def test_private_import_and_fresh_command_after_reload(tmp_path):
    path = tmp_path / unlock.private_filename(ADDRESS)
    path.write_text(json.dumps(valid_import()), encoding="utf-8-sig")
    first = unlock.load_seed(str(path), "99gv5nmz", "fictional-unit", ADDRESS)
    second = unlock.load_seed(str(path), "99gv5nmz", "fictional-unit", ADDRESS)
    assert first == second == SEED
    a = unlock.build_command(first, 1800000000)
    b = unlock.build_command(second, 1800000060)
    assert len(a) == 19 and a[:13] == b[:13] and a[17:] == b[17:]
    assert a[13:17] != b[13:17]
    assert a[18] == 1  # Capture proved outgoing member 1 despite reported DP19=0.


@pytest.mark.parametrize("field,value", [
    ("version", 2), ("version", True), ("product_id", "8gza4o8a"),
    ("device_id", "another-unit"), ("address", "AA:BB:CC:DD:EE:02"),
    ("seed", "not-a-secret"), ("seed", None), ("seed", "00" * 11),
    ("seed", "ffff" + "11" * 8 + "01"),
    ("seed", "0005" + "11" * 8 + "00"),
    ("seed", "0005" + "11" * 8 + "65"),
])
def test_invalid_or_other_unit_import_never_echoes_values(tmp_path, field, value):
    data = valid_import()
    data[field] = value
    path = tmp_path / "private.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="archivo privado") as err:
        unlock.load_seed(str(path), "99gv5nmz", "fictional-unit", ADDRESS)
    assert data["seed"] is None or str(data["seed"]) not in str(err.value)
    assert err.value.__suppress_context__


@pytest.mark.parametrize("contents", ["{PRIVATE", "[]", "null", "x" * 4097])
def test_malformed_private_file_is_redacted(tmp_path, contents):
    path = tmp_path / "private.json"
    path.write_text(contents)
    with pytest.raises(ValueError, match="archivo privado") as err:
        unlock.load_seed(str(path), "99gv5nmz", "fictional-unit", ADDRESS)
    assert "PRIVATE" not in str(err.value)


@pytest.mark.parametrize("product", ["b3aouluh", "okkyfgfs", "7a4xvbtt", "8gza4o8a"])
def test_import_rejects_legacy_before_reading_file(product):
    with pytest.raises(ValueError, match="exclusivo"):
        unlock.load_seed("not-present", product, "fictional-unit", ADDRESS)


@pytest.mark.parametrize("address", ["../secret", "AA:BB:CC:DD:EE:XX", "", None])
def test_private_filename_rejects_paths(address):
    with pytest.raises(ValueError):
        unlock.private_filename(address)


@pytest.mark.parametrize("timestamp", [0, -1, 2**32, True, 1.2])
def test_invalid_timestamp(timestamp):
    with pytest.raises(ValueError):
        unlock.build_command(SEED, timestamp)


@pytest.mark.parametrize("result", range(7))
def test_matching_reply_returns_device_result(result):
    command = unlock.build_command(SEED, 1800000000)
    reply = command[2:4] + command[:2] + command[4:18] + bytes([result])
    assert unlock.response_result(command, reply) == result


@pytest.mark.parametrize("offset", range(18))
def test_unrelated_reply_is_ignored(offset):
    command = unlock.build_command(SEED, 1800000000)
    reply = bytearray(command[2:4] + command[:2] + command[4:18] + b"\x00")
    reply[offset] ^= 1
    assert unlock.response_result(command, bytes(reply)) is None


@pytest.mark.parametrize("reply", [b"", b"0" * 20, None, "private"])
def test_invalid_reply_is_ignored(reply):
    assert unlock.response_result(unlock.build_command(SEED, 1800000000), reply) is None
