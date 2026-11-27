"""SDK V4 reports, authenticated notifications and product isolation."""

import asyncio
from struct import pack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.tuya_ble.tuya_ble import fd50_reports as reports
from custom_components.tuya_ble.tuya_ble.const import TuyaBLECode as Code
from custom_components.tuya_ble.tuya_ble.exceptions import (
    TuyaBLEDataFormatError, TuyaBLEDataLengthError,
)
from custom_components.tuya_ble.tuya_ble.tuya_ble import TuyaBLEDevice

LIVE = Code.FUN_RECEIVE_DP_V4
TIMED = Code.FUN_RECEIVE_TIME_DP_V4
HEADER = bytes.fromhex("00123456780002")


def dp(dp_id, kind, value):
    return bytes([dp_id, kind]) + pack(">H", len(value)) + value


def make_device(product="99gv5nmz"):
    device = TuyaBLEDevice(None, SimpleNamespace(address="DC:23:52:F5:7D:56"))
    device._device_info = SimpleNamespace(product_id=product)
    device._session_key = bytes(16)
    device._client = SimpleNamespace(is_connected=True)
    device._is_paired = True
    return device


async def drain(device):
    tasks = list(getattr(device, "_fd50_report_ack_tasks", ()))
    if tasks:
        await asyncio.gather(*tasks)


def test_live_sdk_header_scalars_unsigned_enum_and_ack():
    payload = HEADER + dp(8, 2, pack(">i", 52)) + dp(47, 1, b"\x01") + dp(21, 4, b"\x80")
    report = reports.parse_report(LIVE, payload, 123.5)
    assert report.timestamp == 123.5
    assert report.flags == 2
    assert [(i, value) for i, _, value in report.datapoints] == [(8, 52), (47, True), (21, 128)]
    assert report.acknowledgement == bytes.fromhex("0012345678000200")


@pytest.mark.parametrize("time_data,expected", [
    (b"\x01" + pack(">I", 1795807200), 1795807200.0),
    (b"\x00" + b"1795807200123", 1795807200.123),
])
def test_timestamped_report_retains_original_time(time_data, expected):
    report = reports.parse_report(TIMED, HEADER + time_data + dp(12, 2, pack(">i", 200)), 42)
    assert report.timestamp == expected
    assert report.acknowledgement == HEADER + b"\x00"


@pytest.mark.parametrize("flags", [0, 1, 128, 129])
@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_response_policy_and_modes(flags, mode):
    header = HEADER[:5] + bytes([flags, mode])
    result = reports.parse_report(LIVE, header + dp(9, 4, b"\x00"), 1)
    assert result.acknowledgement == (None if flags & 128 else header + b"\x00")


def test_all_types_and_raw_larger_than_255():
    payload = (
        HEADER + dp(70, 0, b"x" * 300) + dp(64, 3, "texto".encode())
        + dp(80, 5, b"\xff\x00") + dp(8, 2, pack(">i", -10))
    )
    result = reports.parse_report(LIVE, payload, 1)
    assert [value for _, _, value in result.datapoints] == [b"x" * 300, "texto", b"\xff\x00", -10]


@pytest.mark.parametrize("payload", [
    b"", HEADER[:6], HEADER,
    b"\x01" + HEADER[1:] + dp(9, 4, b"\x00"),
    HEADER[:5] + b"\x02\x00" + dp(9, 4, b"\x00"),
    HEADER[:5] + b"\x00\x04" + dp(9, 4, b"\x00"),
    HEADER + b"\x08\x02\x00",
    HEADER + dp(8, 2, b"\x00"),
    HEADER + dp(47, 1, b"\x02"),
    HEADER + dp(47, 1, b""),
    HEADER + dp(21, 4, b"\x00\x00"),
    HEADER + dp(80, 5, b"\x00\x00\x00"),
    HEADER + dp(64, 3, b"\xff"),
    HEADER + dp(8, 7, b"\x00"),
    HEADER + dp(8, 2, pack(">i", 52)) + b"\x2f",
    HEADER + b"\x08\x02\x00\x04\x00",
])
def test_malformed_live_report_has_no_partial_updates_or_ack(payload):
    async def run():
        device = make_device()
        observed = []
        device.register_callback(lambda values: observed.extend(values))
        with patch.object(reports, "send_packet", AsyncMock()) as sender:
            packets = device._build_packets(7, LIVE, payload)
            for packet in packets:
                device._notification_handler(None, bytearray(packet))
            await drain(device)
        assert len(device.datapoints) == 0
        assert not observed
        sender.assert_not_awaited()
        assert device._fd50_v4_rejected == 1
    asyncio.run(run())


@pytest.mark.parametrize("time_data", [b"", b"\x02", b"\x01\x00", b"\x00abcdefghijklz"])
def test_bad_timestamp_rejected(time_data):
    with pytest.raises((TuyaBLEDataLengthError, TuyaBLEDataFormatError)):
        reports.parse_report(TIMED, HEADER + time_data, 1)


def test_fragmented_report_preserves_repeated_access_and_alarm_ids_and_ack():
    async def run():
        device = make_device()
        observed = []
        device.register_callback(
            lambda values: observed.extend((v.id, v.value, v.timestamp) for v in values)
        )
        payload = HEADER + b"\x01" + pack(">I", 1795807200)
        payload += dp(12, 2, pack(">i", 100)) + dp(12, 2, pack(">i", 200))
        payload += dp(21, 4, b"\x00") + dp(21, 4, b"\x01")
        packets = device._build_packets(73, TIMED, payload)
        with patch.object(reports, "send_packet", AsyncMock()) as sender:
            for packet in packets[:-1]:
                device._notification_handler(None, bytearray(packet))
            assert not observed
            sender.assert_not_awaited()
            device._notification_handler(None, bytearray(packets[-1]))
            await drain(device)
            sender.assert_awaited_once_with(
                device, TIMED, HEADER + b"\x00", 73, False, expected_client=device._client
            )
            # An identical transport retransmission is ACKed but not re-published.
            for packet in packets:
                device._notification_handler(None, bytearray(packet))
            await drain(device)
            assert sender.await_count == 2
        assert observed == [(12, 100, 1795807200), (12, 200, 1795807200),
                            (21, 0, 1795807200), (21, 1, 1795807200)]
        assert device.datapoints[12].value == 200
        assert device._fd50_v4_acks_sent == 2
    asyncio.run(run())


def test_corrupt_crc_and_out_of_order_fragments_never_ack():
    async def run():
        device = make_device()
        packets = device._build_packets(3, LIVE, HEADER + dp(8, 2, pack(">i", 52)))
        with patch.object(reports, "send_packet", AsyncMock()) as sender:
            device._notification_handler(None, bytearray(packets[-1]))
            corrupted = [bytearray(p) for p in packets]
            corrupted[-1][-1] ^= 1
            for packet in corrupted:
                device._notification_handler(None, packet)
            await drain(device)
        sender.assert_not_awaited()
        assert len(device.datapoints) == 0
    asyncio.run(run())


@pytest.mark.parametrize("product", ["b3aouluh", "okkyfgfs", "7a4xvbtt", "8gza4o8a"])
def test_other_products_do_not_enter_v4_handler(product):
    device = make_device(product)
    with patch.object(reports, "handle_report", side_effect=AssertionError("wrong product")):
        device._handle_command_or_response(1, 0, LIVE, HEADER + dp(9, 4, b"\x00"))
    assert len(device.datapoints) == 0


def test_ack_failure_is_consumed_and_report_values_are_not_logged(caplog):
    async def run():
        device = make_device()
        with patch.object(reports, "send_packet", AsyncMock(side_effect=RuntimeError("secret-value"))):
            device._handle_command_or_response(1, 0, LIVE, HEADER + dp(70, 0, b"secret-value"))
            await drain(device)
        assert device._fd50_v4_ack_errors == 1
        assert "secret-value" not in caplog.text
        assert "secret-value" not in str(device._fd50_v4_last_report)
    asyncio.run(run())


def test_ack_cannot_be_sent_on_a_replacement_session():
    async def run():
        device = make_device()
        old_client = device._client
        new_client = SimpleNamespace(is_connected=True, write_gatt_char=AsyncMock())
        device._client = new_client
        await reports._acknowledge(device, old_client, LIVE, HEADER + b"\x00", 3)
        new_client.write_gatt_char.assert_not_awaited()
        assert device._fd50_v4_ack_errors == 1
    asyncio.run(run())


@pytest.mark.parametrize("payload,reason,metadata", [
    (HEADER[:6], "truncated_report_header", {"remaining": 6}),
    (b"\x01" + HEADER[1:] + dp(8, 2, pack(">i", 1)),
     "unsupported_header_version", {"header_version": 1}),
    (HEADER + dp(12, 2, b"\x00\x64"),
     "invalid_value_length", {"dp_id": 12, "dp_type": 2, "declared_length": 2}),
    (HEADER + dp(47, 1, b"\x02"),
     "invalid_bool_encoding", {"dp_id": 47, "dp_type": 1, "declared_length": 1}),
    (HEADER + b"\x08\x02\x00\x04\x00",
     "truncated_dp_value", {"dp_id": 8, "dp_type": 2, "declared_length": 4, "remaining": 1}),
])
def test_rejection_metadata_identifies_structure_without_values(payload, reason, metadata):
    with pytest.raises((TuyaBLEDataLengthError, TuyaBLEDataFormatError)) as caught:
        reports.parse_report(LIVE, payload, 1)
    details = caught.value.report_details
    assert details["reason"] == reason
    assert all(details[key] == value for key, value in metadata.items())
    assert "value" not in details
    assert "payload" not in details


def test_rejection_history_is_bounded_private_and_survives_valid_report(caplog):
    async def run():
        device = make_device()
        secret = b"private-credential-123"
        # Earlier valid raw DP must not be published or copied into diagnostics
        # when a later DP fails validation.
        payload = HEADER + dp(70, 0, secret) + dp(71, 3, b"\xff" + secret)
        caplog.set_level("DEBUG")
        with patch.object(reports, "send_packet", AsyncMock()) as sender:
            for seq in range(25):
                with pytest.raises(TuyaBLEDataFormatError):
                    reports.handle_report(device, seq, LIVE, payload)
            assert len(device._fd50_v4_rejections) == 20
            assert device._fd50_v4_rejections[-1]["dp_id"] == 71
            assert device._fd50_v4_rejections[-1]["reason"] == "invalid_string_encoding"
            assert len(device.datapoints) == 0
            sender.assert_not_awaited()
            no_ack_header = HEADER[:5] + b"\x80\x00"
            reports.handle_report(device, 30, LIVE, no_ack_header + dp(47, 1, b"\x00"))
            await drain(device)
        assert device._fd50_v4_last_report["valid"]
        assert len(device._fd50_v4_rejections) == 20
        combined = str(device._fd50_v4_rejections) + caplog.text
        assert secret.decode() not in combined
        assert secret.hex() not in combined
    asyncio.run(run())


@pytest.mark.parametrize("product", ["b3aouluh", "okkyfgfs", "7a4xvbtt", "8gza4o8a"])
def test_direct_report_diagnostics_are_dlock_only(product):
    device = make_device(product)
    reports.handle_report(device, 1, LIVE, b"")
    assert not hasattr(device, "_fd50_v4_rejections")
    assert not hasattr(device, "_fd50_v4_received")
