"""Offline DLOCK options, privacy and isolation with real HA flow classes."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from custom_components.tuya_ble import config_flow, diagnostics
from custom_components.tuya_ble.dlock_unlock import CONF_DLOCK_SEED, seed_record

SEED = "0005616263646566676801"


def entry_for(unit=1, product="99gv5nmz"):
    return SimpleNamespace(
        entry_id=f"entry-{unit}", title=f"DLOCK {unit}",
        data={"address": f"AA:BB:CC:DD:EE:{unit:02X}"},
        options={"product_id": product, "device_id": f"dlock-{unit}",
                 "connection_mode": "periodic", "sync_interval_minutes": 60},
    )


def flow_for(entry):
    flow = config_flow.TuyaBLEOptionsFlow(entry)
    flow.async_show_menu = MagicMock(side_effect=lambda **kw: kw)
    flow.async_show_form = MagicMock(side_effect=lambda **kw: kw)
    flow.async_create_entry = MagicMock(side_effect=lambda **kw: kw)
    flow.async_abort = MagicMock(side_effect=lambda **kw: kw)
    return flow


def test_dlock_menu_without_login():
    flow = flow_for(entry_for())
    flow.async_step_login = AsyncMock()
    assert asyncio.run(flow.async_step_init())["menu_options"] == ["local_unlock", "login"]
    flow.async_step_login.assert_not_awaited()


@pytest.mark.parametrize("product", ["b3aouluh", "okkyfgfs", "7a4xvbtt", "8gza4o8a"])
def test_other_families_keep_existing_options_path(product):
    flow = flow_for(entry_for(product=product))
    flow.async_step_login = AsyncMock(return_value={"old": True})
    assert asyncio.run(flow.async_step_init()) == {"old": True}
    flow.async_step_login.assert_awaited_once_with(None)
    assert asyncio.run(flow.async_step_local_unlock({CONF_DLOCK_SEED: SEED})) == {"reason": "not_dlock"}
    flow.async_create_entry.assert_not_called()


@pytest.mark.parametrize("unit", [1, 2, 3])
def test_seed_saved_offline_and_bound_to_each_entry(unit):
    entry = entry_for(unit)
    flow = flow_for(entry)
    with patch.object(config_flow, "_try_login", new_callable=AsyncMock) as login:
        result = asyncio.run(flow.async_step_local_unlock({CONF_DLOCK_SEED: SEED}))
    login.assert_not_awaited()
    saved = json.loads(json.dumps(result["data"]))
    assert saved[CONF_DLOCK_SEED] == seed_record(SEED, "99gv5nmz", f"dlock-{unit}", entry.data["address"])
    assert all(saved[k] == v for k, v in entry.options.items())
    assert CONF_DLOCK_SEED not in entry.options  # Options input is not mutated.
    entry.options = saved
    reloaded = flow_for(entry)
    form = asyncio.run(reloaded.async_step_local_unlock())
    assert SEED not in repr(form)
    marker, selector = next(iter(form["data_schema"].schema.items()))
    assert marker.default is vol.UNDEFINED
    assert selector.config["type"] == "password"
    assert asyncio.run(reloaded.async_step_local_unlock({}))["data"] == saved


def test_invalid_seed_is_not_echoed_or_saved():
    flow = flow_for(entry_for())
    invalid = "private-invalid-input"
    form = asyncio.run(flow.async_step_local_unlock({CONF_DLOCK_SEED: invalid}))
    assert form["errors"] == {CONF_DLOCK_SEED: "invalid_dlock_seed"}
    assert invalid not in repr(form)
    flow.async_create_entry.assert_not_called()


def test_seed_redacted_from_config_entry_diagnostics():
    entry = entry_for()
    entry.options[CONF_DLOCK_SEED] = seed_record(SEED, "99gv5nmz", "dlock-1", entry.data["address"])
    entry.as_dict = lambda: {"options": dict(entry.options), "data": dict(entry.data)}
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(SimpleNamespace(data={}), entry))
    assert SEED not in json.dumps(result)
    assert result["options"][CONF_DLOCK_SEED] == "**REDACTED**"
    assert result["entry"]["options"][CONF_DLOCK_SEED] == "**REDACTED**"
