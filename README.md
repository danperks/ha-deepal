# Changan Deepal Cloud for Home Assistant

<p align="center">
  <img src="icon.png" alt="Deepal logo" width="160">
</p>

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)

Custom Home Assistant integration for the Changan Deepal cloud API.

This integration was built against a UK-market Deepal S07. At the moment, it should be treated as **S07-only**. Login is supported for UK and Portugal accounts, but Portugal support is newly added and needs wider testing. Other models, countries, app regions, and API variants are untested.

## Important Warnings

- Use this integration at your own risk.
- This is an unofficial integration and is not endorsed by Changan or Deepal.
- Remote commands can affect the vehicle. Make sure it is safe before using controls such as locks, windows, boot, climate, lights, or horn.
- This integration cannot be used to drive the car. It does not implement the BLE/digital key path required for drive authorization.
- Logging in through this integration can log you out of the official Deepal app or other devices. Likewise, logging back into the official app may invalidate the Home Assistant session.

## Supported Vehicle

- Deepal S07
- Login regions: United Kingdom, Portugal

## Current Features

- Phone/SMS login flow through Home Assistant.
- Native Home Assistant reauthentication/repair flow when the cloud session is invalidated.
- Vehicle telemetry sensors and binary sensors.
- Manual refresh button.
- Cabin climate entity.
- Charge limit and charging schedule controls.
- Door lock control.
- Window and boot cover controls.
- Flash lights and horn buttons.

Some controls are still being reverse engineered and may not work reliably on every vehicle/app session.

## Installation

### HACS

1. Open HACS in Home Assistant.
2. Go to **Integrations**.
3. Open the three-dot menu and choose **Custom repositories**.
4. Add `https://github.com/danperks/ha-deepal` as an **Integration** repository.
5. Install **Changan Deepal Cloud** from HACS.
6. Restart Home Assistant.

### Manual

1. Copy `custom_components/deepal` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up Changan Deepal Cloud.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=deepal)

You can also configure it manually from **Settings -> Devices & services -> Add integration**, then search for **Changan Deepal Cloud**.

During setup you can choose whether to enable remote commands. Remote commands require the same control PIN used by the official Deepal app.

## Notes

- The integration polls cached cloud status every minute and asks for refreshed vehicle data every 5 minutes when remote commands are enabled.
- After a command is accepted, the integration briefly polls for command result and refreshed vehicle state so Home Assistant updates faster than the normal polling interval.
- If the account is used elsewhere, Home Assistant may need reauthentication.

## Development Status

This is early reverse-engineering work. Expect breaking changes, incomplete model support, and occasional cloud API/session issues.
