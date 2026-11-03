"""Check the experimental LA-T01 DP71 request without a physical lock."""

import base64
import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tuya_ble"
    / "la_t01_unlock.py"
)
spec = importlib.util.spec_from_file_location("la_t01_unlock", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
LA_T01UnlockDataError = module.LA_T01UnlockDataError
build_dp71_unlock = module.build_dp71_unlock


def properties(*, member=1, result=0, pairing_time=10, report_time=20):
    report = (
        b"\x00\x05\xff\xff"
        + b"ABCDEFGH"
        + b"\x01"
        + (1700000000).to_bytes(4, "big")
        + b"\x00"
        + bytes((result,))
    )
    return [
        {"code": "check_code_set", "time": pairing_time},
        {"code": "unlock_ble", "value": member},
        {
            "code": "ble_unlock_check",
            "value": base64.b64encode(report).decode(),
            "time": report_time,
        },
    ]


def test_dp71_swaps_ids_and_uses_fresh_timestamp_and_member():
    command = build_dp71_unlock(properties(), 1800000000)
    assert len(command) == 19
    assert command[:4] == b"\xff\xff\x00\x05"
    assert command[4:12] == b"ABCDEFGH"
    assert command[12] == 1
    assert int.from_bytes(command[13:17], "big") == 1800000000
    assert command[17:] == b"\x00\x01"


@pytest.mark.parametrize(
    "values",
    (
        lambda items: items[:2],
        lambda items: items.__setitem__(1, {"code": "unlock_ble", "value": 0}),
        lambda items: items.__setitem__(2, {**items[2], "value": "broken"}),
        lambda items: items.__setitem__(
            2, {**items[2], "value": base64.b64encode(b"short").decode()}
        ),
        lambda items: items.__setitem__(0, {"code": "check_code_set", "time": 30}),
    ),
)
def test_dp71_rejects_missing_or_invalid_cloud_reports(values):
    items = properties()
    changed = values(items)
    if changed is not None:
        items = changed
    with pytest.raises(LA_T01UnlockDataError):
        build_dp71_unlock(items, 1800000000)


def test_dp71_rejects_failed_device_response():
    with pytest.raises(LA_T01UnlockDataError, match="apertura válida"):
        build_dp71_unlock(properties(result=4), 1800000000)
