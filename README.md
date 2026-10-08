# Changan Deepal Cloud for Home Assistant

<p align="center">
  <img src="icon.png" alt="Deepal logo" width="160">
</p>

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)

Custom Home Assistant integration for the Changan Deepal cloud API.

This integration was built against a UK-market Deepal S07 and a Portugal-market Deepal S05. Both support telemetry and, when enabled, remote controls. S05 telemetry and door, window, and boot controls use the app's MQTT path.

## Important Warnings

- Use this integration at your own risk.
- This is an unofficial integration and is not endorsed by Changan or Deepal.
- Remote commands can affect the vehicle. Make sure it is safe before using controls such as locks, windows, boot, climate, lights, or horn.
- This integration cannot be used to drive the car. It does not implement the BLE/digital key path required for drive authorization.
- Logging in through this integration can log you out of the official Deepal app or other devices. Likewise, logging back into the official app may invalidate the Home Assistant session.

## Supported Vehicle

- Deepal S07: telemetry and optional remote controls.
- Deepal S05: telemetry and optional remote controls (no charge limit or charging schedule).
- Login regions: United Kingdom, Israel, Portugal, Spain, Italy, Netherlands, Australia

## Current Features

- Email-code and phone/SMS login flows through Home Assistant.
- Native Home Assistant reauthentication/repair flow when the cloud session is invalidated, plus a "Reauthenticate now" option.
- Vehicle telemetry sensors and binary sensors.
- Vehicle image URL sensor from the Deepal vehicle metadata.
- Manual refresh button.
- Cabin climate control.
- Door lock, window, and boot controls. On the S05 these wake the car first and wait for its response.
- Flash lights and horn buttons.
- Driver and front passenger seat heating and ventilation levels.
- Steering wheel heating switch.
- S07 charge limit and charging schedule controls.

S05 controls are new and have only been tested on a few cars, so please report anything that doesn't work.

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

During setup, choose the same login method you use in the official Deepal app. If phone/SMS login says the account is not registered, try email-code login instead. You can choose whether to enable remote commands; remote commands require the same control PIN used by the official Deepal app. Existing S05 entries can enable them from the integration's **Configure** dialog.

## Notes

- The integration polls cached cloud status every minute. S07 can also ask for refreshed vehicle data every 5 minutes when remote commands are enabled. S05 reads refreshed MQTT telemetry, and re-reads it after each command.
- After a command is accepted, the integration briefly polls for command result and refreshed vehicle state so Home Assistant updates faster than the normal polling interval.
- If the account is used elsewhere, Home Assistant may need reauthentication.

## Development Status

This is early reverse-engineering work. Expect breaking changes, incomplete model support, and occasional cloud API/session issues.

## Thanks

This integration is much better thanks to work from the community:

- [@kobizz](https://github.com/kobizz) for the S05 gateway token fix and the S05 door, window, and boot controls.
- [@BeauGiles](https://github.com/BeauGiles) for remote command signing and control PIN fixes, transient request retries, the reauthenticate option, and repair flow.
- [@DylanTusler](https://github.com/DylanTusler) for Australian login support via the Singapore gateway.
- The [ha-deepal-alternative](https://github.com/Sunek0/ha-deepal-alternative) contributors, whose research informed the S05 gateway token handling, S05 climate and light controls, and the seat and steering wheel controls.
