"""Offline DLOCK-X1 verification import and DP71 format; no cloud or DP70 I/O."""

from __future__ import annotations

import json
from pathlib import Path
import re

PRODUCT_ID = "99gv5nmz"
CONF_DLOCK_SEED = "dlock_dp71_seed"


def parse_seed(value: str) -> bytes:
    """Validate a manually supplied verification without echoing it in errors."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{22}", value.strip()):
        raise ValueError("Seed DP71 no válido")
    seed = bytes.fromhex(value.strip())
    build_command(seed, 1)
    return seed


def seed_record(value: str, product_id: str, device_id: str, address: str) -> dict:
    """Bind manually entered verification to this existing HA device identity."""
    private_filename(address)
    if product_id != PRODUCT_ID or not isinstance(device_id, str) or not device_id:
        raise ValueError("Identidad DLOCK-X1 no válida")
    return {"version": 1, "product_id": PRODUCT_ID, "device_id": device_id,
            "address": address.upper(), "seed": parse_seed(value).hex()}


def configured_seed(data: dict, product_id: str, device_id: str, address: str) -> bytes:
    """Refuse verification copied from a different entry or rebound device."""
    if (
        product_id != PRODUCT_ID or not device_id or not isinstance(data, dict)
        or type(data.get("version")) is not int or data["version"] != 1
        or data.get("product_id") != PRODUCT_ID
        or data.get("device_id") != device_id
        or data.get("address") != address.upper()
    ):
        raise ValueError("Seed guardado no válido para esta cerradura")
    return parse_seed(data.get("seed"))


def private_filename(address: str) -> str:
    """Use only a validated Bluetooth address in the private filename."""
    if not isinstance(address, str) or not re.fullmatch(
        r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", address
    ):
        raise ValueError("Dirección Bluetooth no válida")
    return f"dlock_{address.replace(':', '').lower()}.json"


def load_seed(path: str, product_id: str, device_id: str, address: str) -> bytes:
    """Read a bounded private file off the event loop; never expose its content."""
    if product_id != PRODUCT_ID:
        raise ValueError("Control exclusivo de DLOCK-X1")
    private_filename(address)
    try:
        with Path(path).open("r", encoding="utf-8-sig") as source:
            contents = source.read(4097)
        if len(contents) > 4096:
            raise ValueError
        data = json.loads(contents)
        return configured_seed(data, product_id, device_id, address)
    except (OSError, UnicodeError, ValueError):
        raise ValueError(
            "No se ha definido un seed válido para esta cerradura. "
            "Añádelo en Tuya BLE → Configurar → Apertura local"
        ) from None


def build_command(seed: bytes, timestamp: int) -> bytes:
    """Build a fresh request from captured lock ID, verification and member ID."""
    if (
        not isinstance(seed, bytes) or len(seed) != 11
        or not 1 <= int.from_bytes(seed[:2], "big") <= 10000
        or not 1 <= seed[10] <= 100
        or type(timestamp) is not int or not 0 < timestamp < 2**32
    ):
        raise ValueError("Preparación DP71 local no válida")
    return b"\xff\xff" + seed[:10] + b"\x01" + timestamp.to_bytes(4, "big") + b"\x00" + seed[10:]


def response_result(command: bytes, response: bytes) -> int | None:
    """Match the application response, including verification and timestamp."""
    if (
        not isinstance(response, bytes) or len(response) != 19
        or response[:2] != command[2:4]
        or response[2:4] != command[:2]
        or response[4:18] != command[4:18]
        or response[18] > 6
    ):
        return None
    return response[18]
