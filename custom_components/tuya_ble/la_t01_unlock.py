"""Build an experimental LA-T01 DP71 command from its last cloud report.

The cloud report contains the phone's central identifier and random number.
Never log the report, command, or random number.
"""

from __future__ import annotations

import base64
import binascii


class LA_T01UnlockDataError(ValueError):
    """The cloud has no usable LA-T01 DP71 unlock report."""


def build_dp71_unlock(properties: list[dict], timestamp: int) -> bytes:
    """Build one fresh 19-byte mobile unlock request from a successful report."""
    by_code = {item.get("code"): item for item in properties if isinstance(item, dict)}
    report_property = by_code.get("ble_unlock_check")
    member_property = by_code.get("unlock_ble")
    pairing_property = by_code.get("check_code_set")
    if not report_property or not member_property:
        raise LA_T01UnlockDataError("Faltan DP71 o DP19 en Tuya Cloud")
    if pairing_property and pairing_property.get("time", 0) > report_property.get(
        "time", 0
    ):
        raise LA_T01UnlockDataError("La vinculación DP70 es posterior al último DP71")

    encoded = report_property.get("value")
    if not isinstance(encoded, str):
        raise LA_T01UnlockDataError("La respuesta DP71 no contiene datos")
    try:
        report = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as err:
        raise LA_T01UnlockDataError("La respuesta DP71 no es base64 válido") from err
    if len(report) != 19:
        raise LA_T01UnlockDataError("La respuesta DP71 no tiene 19 bytes")

    peripheral_id = int.from_bytes(report[0:2], "big")
    if not (1 <= peripheral_id <= 10000 and report[2:4] == b"\xff\xff"):
        raise LA_T01UnlockDataError("La respuesta DP71 no pertenece al móvil")
    if report[12] != 1 or report[17] != 0 or report[18] != 0:
        raise LA_T01UnlockDataError(
            "La última respuesta DP71 no fue una apertura válida"
        )
    member_id = member_property.get("value")
    if type(member_id) is not int or not 1 <= member_id <= 100:
        raise LA_T01UnlockDataError("DP19 no contiene un miembro válido")
    if not 0 < timestamp < 2**32:
        raise LA_T01UnlockDataError("La fecha actual no cabe en DP71")

    return (
        b"\xff\xff"  # mobile central ID
        + report[0:2]  # lock peripheral ID
        + report[4:12]  # central random number from the successful report
        + b"\x01"  # unlock
        + timestamp.to_bytes(4, "big")
        + b"\x00"  # mobile app method
        + bytes((member_id,))
    )
