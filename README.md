# Tuya BLE CFM

Specialized Home Assistant custom integration for the Tuya BLE locks used in this installation.

This fork intentionally keeps a narrow scope instead of following the complete upstream device catalogue. Its priority is preserving the BLE behaviour that has been validated on the physical locks, especially low battery consumption and device-originated state updates.

## Supported locks

Only these two Tuya product IDs are intentionally supported:

- `okkyfgfs` — P196_V (`ms`)
- `b3aouluh` — Smart Lock (`jtmspro`)

The current installation uses one `okkyfgfs` and four `b3aouluh` locks.

## Exposed entities

The fork keeps only the platforms needed by these locks:

- Button: DP6 `bluetooth_unlock`
- Select: DP31 `beep_volume`
- Binary sensor: DP47 `lock_motor_state`
- Sensor: DP21 `alarm_lock`
- Battery: DP8 on `okkyfgfs`, DP9 `battery_state` on `b3aouluh`
- RSSI diagnostic sensor

## BLE power saving

The integration uses the hardware-tested CFM power saver. Locks disconnect their GATT link after 30 seconds of inactivity and reconnect when required, while keeping the legacy Tuya BLE transport that has already been validated on the real hardware.

The newer upstream 0.12.x transport is deliberately not used in this branch because hardware testing showed regressions with device-originated lock state and DP21 alarm events.

## Installation with HACS

1. Open HACS in Home Assistant.
2. Add `https://github.com/carferrer/tuya-ble-cfm` as a custom repository of type **Integration**.
3. Install **Tuya BLE CFM**.
4. Restart Home Assistant.
5. Add or reload the Tuya BLE integration from **Settings > Devices & services**.

Published GitHub releases include `tuya_ble.zip`, which HACS uses for installation and upgrades.

## Validation

Pull requests are checked with Home Assistant Hassfest, HACS validation, Ruff and Pytest. Regression tests also verify that only the two supported product IDs and the tested BLE transport remain enabled.

## Credits

Derived from the `ha-tuya-ble/ha_tuya_ble` project and its contributors.

## License

MIT License. See [LICENSE](LICENSE).

## Entity IDs and BLE recovery

Entity constructors now suggest IDs in their own platform domain (`button`,
`binary_sensor`, `select`, or `sensor`). Existing `unique_id` values are unchanged.
Home Assistant already registers unique-ID entities under their platform domain,
even when an integration suggests a different prefix. It reuses the registered
ID (including custom names and collision suffixes) on reload. No registry entries
are deleted or renamed, and automation references do not need migration. The
previous `sensor.*` suggestions were the source of the domain deprecation warning,
not evidence that buttons were registered as sensors.

The BLE receiver discards malformed or incomplete frames and resumes at the next
start fragment. Recoverable ordering interruptions are logged at debug level;
invalid lengths, CRCs and payloads are warnings. Valid fragmented frames still
reassemble normally. DP69 handshake, access timestamps, event mappings and
persistent deduplication remain unchanged.
