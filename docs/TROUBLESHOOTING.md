# Troubleshooting

Symptom → likely cause → fix. References point into `HANDOFF.md` and `docs/FIRMWARE-NOTES.md` for the full background.

## The app doesn't appear in the launcher

The folder must be exactly `/system/apps/tfl/` containing `__init__.py` **and** a file literally named `icon.png`. The launcher skips folders missing either. After copying, eject the disk safely and press RESET so the launcher rescans. (`HANDOFF.md` §3–4)

## "WiFi failed - press A to retry"

The badge couldn't join the network within ~10 seconds. Check `/system/secrets.py` exists with correct `WIFI_SSID` / `WIFI_PASSWORD`, and that the network is **2.4 GHz**. This screen only appears when there's no cached data at all; once the app has fetched successfully at least once, offline periods show the stale banner instead.

## "Stale - N min ago - press A"

Working as designed: the last refresh failed (WiFi or TfL), so the app is showing the most recent good data with its age. It clears automatically on the next successful refresh; press A to force one immediately.

## Weather footer has disappeared

Also by design: the footer is removed once the weather data is older than `WEATHER_MAX_STALE_MINS` (default 60 min) *and* the retry burst has been exhausted — a failed hourly fetch is retried 3 more times at the next wakes (2 minutes apart) before the footer drops, so a brief wttr.in blip doesn't cost you the footer for an hour. During a sustained outage the app then retries at most hourly. Press A to force a retry at any time.

## The screen says "Buses are still running - I'm asleep to conserve battery"

Quiet hours, working as designed: between `QUIET_START` and `QUIET_END` (default 22:30–05:30) the app stops fetching bus data to conserve battery, waking only hourly to refresh the header clock/battery and the weather footer. **Press A** for 30 minutes of normal live updates; automatic updates resume at `QUIET_END`. To disable the feature, set `QUIET_START` or `QUIET_END` to `None` (see `docs/CONFIGURATION.md`). If the badge seems parked at the *wrong* time of day, check the header clock — quiet hours follow the RTC, though they're automatically skipped when the clock looks unset (year 2000 after a full battery death).

## The header names a different stop, with a "… Closed" banner

By design: TfL has reported your `STOP_ID` closed. The app fails over to `ALT_STOP_ID` (named in the header) and draws an inverted `{STOP_NAME} Closed` bar above the weather line, so the arrivals shown are for the neighbour stop you can actually board at. It reverts automatically once TfL clears the closure. To keep it on your own stop and show the notice only, set `ALT_STOP_ID = None`. If a closure looks stuck on screen after the stop reopened, note the state is *held* deliberately on failed fetches and clears only on a successful all-clear — press **A** to force a fresh check. (`HANDOFF.md` §9.18)

## The clock shows 00:00 and the date says Sat 01/01

The battery fully died, which resets the RTC to its year-2000 epoch (1 Jan 2000 was a Saturday). The app detects this (`year < RTC_MIN_VALID_YEAR`) and re-syncs from NTP on the next successful connection — charge it, let it refresh once, and the clock self-heals. Since this build stores **UTC** in the RTC and derives UK local time at read time, the recovery is now exact year-round — the old "1 hour behind during BST" caveat is gone. (§9.13, §9.16)

## The clock is a couple of minutes slow

RTC drift: measured at 1 min 40 s slow over roughly three months, and the app only re-syncs after a full battery death. The bus minutes are unaffected (they come from TfL's relative `timeToStation`), but a slow header clock makes the table look staler than it is. Re-seed from the REPL: copy `tools/set_clock.py` to `/system/apps/tfl/`, stop the app in Thonny, run `exec(open("/system/apps/tfl/set_clock.py").read())`, and expect `rtc after` to read UTC (an hour behind your watch in summer; `london_now()` adds the hour for the display). Then press RESET and check the header against your watch. (§5.4, §12)

## The clock is exactly 1 hour off

On this build the RTC stores **UTC** and `london_now()` adds the BST offset at read time, so a persistent one-hour error almost always means one of:

- **The one-time UTC migration hasn't run yet.** The first time you flash this build over an older (local-time) install, it re-seeds the RTC to UTC on the first refresh that gets WiFi up, then latches `persist["utc_migrated"]`. If that first refresh couldn't reach WiFi/NTP, the old local value is briefly read as if it were UTC and shows +1h until the next successful refresh. Press A (or wait for WiFi) and it corrects itself.
- **Local time was written into the RTC.** The chip must hold UTC. Writing local time makes the header read an hour fast in summer, because `london_now()` adds the BST hour on top. Re-seed with `tools/set_clock.py`.
- **You added a second offset.** Don't apply a timezone offset anywhere except inside `london_now()` — every other read is already local. Adding one in display code, or reading `rtc.datetime()` (which is now UTC) and treating it as local, double-counts.

The twice-yearly clocks-change (last Sunday of March / October) needs **no** action on this build — `london_now()` recomputes the offset from the UTC instant on every read, so the switch happens on its own. (§9.16)

## The screen is frozen on old data and the button does nothing

Check the battery first: the two causes look the same on screen.

- **The battery is draining fast (or the badge died soon after).** The chip hung while awake: on firmware v2.0.2 the deep-sleep call occasionally fails and locks up with the chip powered. The current code arms the chip's hardware watchdog on battery, so the badge reboots itself back into the app within about 16 seconds and skips one refresh. If you see this freeze on unmodified current code, please open an issue. (§9.19)
- **The battery is fine and a reset revives it instantly.** The device deep-slept without a wake source, historically caused by a silently failed `rtc.set_alarm()`. The current code arms the alarm with a verified retry and clears stale alarm flags at startup, which fixed the original ~48 h freeze. If you hit this on unmodified current code, please open an issue with how long it ran. (§9.12)

## The screen went completely blank after the battery died

A brownout mid-refresh: power failed between `screen.clear()` and the redraw, and e-ink holds whatever was last pushed — in this case, nothing. The low-battery pause exists to prevent exactly this (it stops all refresh activity well before collapse). If you see a blank panel, the cell likely drained faster than the `LOW_BATT_VOLTS` threshold anticipated — consider calibrating it upward for your cell (`docs/CONFIGURATION.md`). Charging and relaunching recovers it. (§9.13–9.14)

## Battery percentage seems wrong / jumps around

The percentage is interpolated from cell voltage through a generic LiPo curve; cells vary, and voltage sags under load. While charging it deliberately shows `chg` (the charger holds the voltage high, so a % would be fiction). If yours reads badly, calibrate `_LIPO_CURVE` — see `docs/CONFIGURATION.md`. Don't switch to `badge.battery_level()`: on this firmware it only ever returns 0 or 100. (§9.10)

## Grey areas render as solid black

You're drawing grey under the default fast update mode. The panel has two native greys but renders them cleanly only under `MEDIUM_UPDATE`/`FULL_UPDATE`; also note `color.grey` is the *dark* grey — the light one is `color.smoke`. (§9.15)

## Thonny can't see the badge

Use a USB-C **data** cable; charge-only cables enumerate nothing. The badge must be running normally (not in disk mode), interpreter set to *MicroPython (Raspberry Pi Pico)*, then hit Stop to get the `>>>` prompt. (§2)

## Wrong stop / want a different stop

Edit `STOP_ID` (and `STOP_NAME`) — see the README's "Finding your stop ID". Note the trailing letter on Naptan IDs distinguishes direction/side of the road, so double-check you've picked the stop for the direction you actually travel.
