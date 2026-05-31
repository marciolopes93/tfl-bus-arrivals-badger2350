# Badger 2350 TFL Bus + Weather App

A MicroPython app for the [Pimoroni Badger 2350](https://shop.pimoroni.com/products/badger-2350)
e-ink badge that shows **live London bus arrival times** for a single
stop, plus a one-line **weather summary**. It auto-refreshes on a timer, degrades
gracefully when offline (keeps showing last-known data with a stale warning), and
displays the battery level. 🚌

> **Requires Badgeware firmware v2.0.0 or later.** The app uses the v2.x API
> (`update`/button-press/`mode` calls changed in v2.0). It will **not** run on
> the older BadgerOS "v4" firmware.

![App icon](icon.png)

## What it does

- Live bus arrivals for a configured TFL stop (default: **Abbey Road**, `490003028E`)
- One-line weather footer for a configured location (default: **Westminster**)
- Auto-refresh every 2 minutes (press **A** for an instant refresh)
- Survives deep-sleep via `State` persistence; shows stale data + warning when offline
- Battery percentage in the header

## Files

| File | Purpose |
|------|---------|
| `__init__.py` | The app. Deploys to `/system/apps/tfl/__init__.py` on the badge. |
| `icon.png` | 24×24 launcher icon. Deploys to `/system/apps/tfl/icon.png`. |
| `HANDOFF.md` | Full source-of-truth doc: hardware, Badgeware v2.x API notes, APIs, UI layout, bugs fixed. **Read this before changing code.** |

## Quick start

1. Put the badge in USB disk mode.
2. Copy `__init__.py` and `icon.png` into `apps/tfl/` on the disk
   (this is `/system/apps/tfl/` on the device).
3. Ensure `/system/secrets.py` has `WIFI_SSID` and `WIFI_PASSWORD`.
4. Safely eject, press **RESET**, launch **TFL** from the menu.

See [`HANDOFF.md`](HANDOFF.md) for the full setup, API reference, and config knobs.

## Configuration

Knobs live at the top of `__init__.py`: `STOP_ID`, `STOP_NAME`, `MAX_BUSES`,
`REFRESH_MINS`, `WEATHER_MAX_STALE_MINS`, `WEATHER_LOCATION`, `REQ_TIMEOUT`.

## Data sources

- **Buses:** [TFL Unified API](https://api.tfl.gov.uk/) — no key required at this volume.
- **Weather:** [wttr.in](http://wttr.in/) JSON endpoint (HTTP, not HTTPS — see HANDOFF.md).
