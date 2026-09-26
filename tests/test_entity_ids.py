"""Test actual constructors in isolation from Home Assistant's runtime."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components/tuya_ble"


def load_class(namespace, filename, name, bases=None):
    tree = ast.parse((ROOT / filename).read_text())
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    )
    cls.body = [
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    ]
    if bases is not None:
        cls.bases = [ast.Name(id=base, ctx=ast.Load()) for base in bases]
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            cls,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), filename, "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize(
    "platform,name,key",
    [
        ("button", "TuyaBLEButton", "bluetooth_unlock"),
        ("binary_sensor", "TuyaBLEBinarySensor", "lock_motor_state"),
        ("select", "TuyaBLESelect", "beep_volume"),
        ("sensor", "TuyaBLESensor", "alarm_lock"),
    ],
)
def test_platform_constructors_preserve_unique_id_and_suggest_correct_domain(
    platform, name, key
):
    class CoordinatorEntity:
        def __init__(self, coordinator):
            pass

    calls = []

    def generate_entity_id(format, unique_id, *, hass):
        calls.append((format, unique_id, hass))
        return format.format(unique_id.replace("-", "_"))

    namespace = {
        "asyncio": asyncio,
        "CoordinatorEntity": CoordinatorEntity,
        "generate_entity_id": generate_entity_id,
        "get_device_info": lambda device: {},
    }
    load_class(namespace, "devices.py", "TuyaBLEEntity")
    entity_cls = load_class(namespace, platform + ".py", name, ["TuyaBLEEntity"])
    description = SimpleNamespace(key=key, translation_key=None, options=[])
    device = SimpleNamespace(device_id="unchanged-device")
    hass = object()
    entity = entity_cls(
        hass, None, device, None, SimpleNamespace(description=description, options=[])
    )
    assert entity._attr_unique_id == "unchanged-device-" + key
    assert calls == [(platform + ".{}", entity._attr_unique_id, hass)]
    assert entity.entity_id.startswith(platform + ".")
    # Registry identity is (platform domain, tuya_ble, unique_id), unchanged.
    # HA resolves this key to the existing entry, including user-renamed IDs;
    # constructors must not remove or recreate entries or rewrite automations.
