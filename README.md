# Changan Deepal Cloud for Home Assistant

Custom Home Assistant integration for the Changan Deepal cloud API.

This integration was built against a UK-market Deepal S07. At the moment, it should be treated as **S07-only** and **UK-only**. Other models, countries, app regions, and API variants are untested.

## Important Warnings

- Use this integration at your own risk.
- This is an unofficial integration and is not endorsed by Changan or Deepal.
- Remote commands can affect the vehicle. Make sure it is safe before using controls such as locks, windows, boot, climate, lights, or horn.
- This integration cannot be used to drive the car. It does not implement the BLE/digital key path required for drive authorization.
- Logging in through this integration can log you out of the official Deepal app or other devices. Likewise, logging back into the official app may invalidate the Home Assistant session.

## Supported Vehicle

- Deepal S07, UK market

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

1. Copy `custom_components/deepal` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Go to **Settings -> Devices & services -> Add integration**.
4. Search for **Deepal / Changan Cloud**.
5. Follow the phone/SMS login flow.

During setup you can choose whether to enable remote commands. Remote commands require the same control PIN used by the official Deepal app.

## Notes

- The integration polls the cloud API every 5 minutes by default.
- After a command is accepted, the integration briefly polls for command result and refreshed vehicle state so Home Assistant updates faster than the normal polling interval.
- If the account is used elsewhere, Home Assistant may need reauthentication.

## Development Status

This is early reverse-engineering work. Expect breaking changes, incomplete model support, and occasional cloud API/session issues.
