# Tuya BLE CFM

Custom Home Assistant integration for Tuya BLE devices, maintained as a CFM fork with a particular focus on Tuya BLE locks.

This project is derived from the Tuya BLE integration maintained by the `ha-tuya-ble` project. The fork keeps the `tuya_ble` Home Assistant domain so existing installations can continue to use the same integration.

## Installation with HACS

1. Open HACS in Home Assistant.
2. Add `https://github.com/carferrer/tuya-ble-cfm` as a custom repository of type **Integration**.
3. Install **Tuya BLE CFM**.
4. Restart Home Assistant.
5. Add or reload the Tuya BLE integration from **Settings > Devices & services**.

Published GitHub releases include `tuya_ble.zip`, which HACS uses for installation and upgrades.

## CFM lock support

The current CFM branch is based on Tuya BLE 0.12.1. Battery-powered devices in the `ms` and `jtmspro` lock categories use the upstream native on-demand BLE connection policy by default: Home Assistant connects when an operation is needed and disconnects after the configured idle delay. An explicit `keep_connection` option always takes precedence.

In addition to the lock models already supported upstream, this fork preserves support for the `jtmspro` products `8gza4o8a` and `b3aouluh`, including their tested unlock, volume, motor-state, alarm and battery-state datapoints.

## Validation

Pull requests are checked with:

- Home Assistant Hassfest
- HACS validation
- Ruff
- Pytest smoke tests

Renovate is used to propose dependency and GitHub Actions updates.

## Credits

Based on the work of the `ha-tuya-ble/ha_tuya_ble` project and its contributors.

## License

MIT License. See [LICENSE](LICENSE).
