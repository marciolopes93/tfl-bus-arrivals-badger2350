# Configuration reference

All settings live at the top of `__init__.py` unless noted otherwise. After editing, re-copy the file to `/system/apps/tfl/` in disk mode and press RESET.

## Required

| Variable | Default | Notes |
|----------|---------|-------|
| `STOP_ID` | `490003028E` | TfL Naptan ID of the stop to monitor. See the README for how to find yours. |
| `STOP_NAME` | `Abbey Road` | Display label in the header. Keep it short — the header also holds the battery meter and clock. |
| `WIFI_SSID` / `WIFI_PASSWORD` | — | **Not in this file** — they live in `/system/secrets.py` (see `secrets.py.example`). 2.4 GHz networks only. |

## Refresh & display

| Variable | Default | Notes |
|----------|---------|-------|
| `MAX_BUSES` | `6` | Arrival rows shown. 6 fills the space between the header and the weather footer at the current 16 px row height. |
| `REFRESH_MINS` | `2` | Minutes between automatic refreshes. TfL predictions are minute-precision, so going below 2 buys little; raising it is the single biggest battery saving. |
| `WEATHER_LOCATION` | `Westminster` | Any place name wttr.in understands. Used only to build the fetch URL — the footer shows the date and condition, not the location. |
| `WEATHER_MAX_STALE_MINS` | `60` | Does double duty: weather is fetched at most this often (success **or** failure — a downed wttr.in is retried hourly, not every cycle), and the footer is removed once the data is older than this *and* the retry burst below is exhausted. |
| `WEATHER_RETRY_BURST` | `3` | When the hourly weather fetch fails, retry this many more times at the next wakes (2 min apart, alongside the bus fetch) before giving up. The footer stays on screen through the burst and only drops once it's exhausted; attempts then return to hourly (or press A). |
| `AWAKE_MS` | `400` | How long the device stays fully awake (CPU + radio on) after drawing, before deep-sleeping. A button press wakes it from deep sleep anyway, so a longer window adds battery cost without adding responsiveness. Going much below ~200 ms saves little — the WiFi connect, fetch and e-ink push dominate each cycle's awake time. |

## Quiet hours

| Variable | Default | Notes |
|----------|---------|-------|
| `QUIET_START` | `"22:30"` | Start of the overnight pause window, as an `"HH:MM"` string. Set this **or** `QUIET_END` to `None` to disable quiet hours entirely. |
| `QUIET_END` | `"05:30"` | End of the window (exclusive — the first normal refresh lands at this time). Midnight wraparound is supported, so `"22:30"` → `"05:30"` works as expected. |
| `QUIET_SLEEP_MINS` | `60` | How often the device wakes during quiet hours for a heartbeat: refresh weather if due, repaint the header clock/battery and footer. Also the deep-sleep chunk length (the final chunk is shortened so the wake at `QUIET_END` is exact). |
| `QUIET_OVERRIDE_MINS` | `30` | Pressing **A** during quiet hours resumes normal live updates for this long, after which the badge parks again if still inside the window. |

Inside the window the app does no bus fetching at all — roughly 28 fetch cycles an evening replaced by one weather-only heartbeat per hour. Two robustness guards: quiet hours are skipped entirely while the clock looks unset (after a full battery death the RTC reads year 2000, and a device that trusted that 00:00 would park itself forever instead of reconnecting to fix its own clock), and a button press always punches through.

On USB power the badge never deep-sleeps, so quiet hours still show the parked screen but the loop keeps spinning; the heartbeat gate prevents e-ink churn. Battery conservation is irrelevant on USB, so if you dock the badge overnight and would rather it stay live, set either bound to `None`.

## Network

| Variable | Default | Notes |
|----------|---------|-------|
| `REQ_TIMEOUT` | `8` | Seconds before an HTTP request gives up. Without it, a dead router can hang a request for minutes with the screen frozen. Applied both as the socket default and as the `requests.get` kwarg. Resist lowering it: the timeout only bites on *failed* requests (healthy fetches return in 1–3 s regardless), while the TLS handshake to the TfL API can be slow on a cold connection — a tighter value invites spurious stale banners. |

## Clock recovery

| Variable | Default | Notes |
|----------|---------|-------|
| `RTC_MIN_VALID_YEAR` | `2024` | If the RTC reports a year below this, the clock is treated as wiped (a full battery death resets it to the year-2000 epoch) and an NTP re-sync is triggered on the next successful connection. Normal wakes never touch NTP. |

Known limitation: the NTP seed is UTC and this firmware has no setter to write a corrected local time, so a battery death recovered during British Summer Time leaves the clock 1 hour behind until set manually. Winter recoveries are correct. Details in `HANDOFF.md` §9.16.

## Low-battery behaviour

| Variable | Default | Notes |
|----------|---------|-------|
| `LOW_BATT_VOLTS` | `3.45` | At/below this (on battery, with a valid reading) the app draws a static low-battery screen once and pauses all WiFi/fetch/redraw activity. |
| `LOW_BATT_RESUME_VOLTS` | `3.60` | Hysteresis: it auto-resumes only above this voltage, or whenever a charger is detected — so it can't flap at the threshold. |
| `LOW_POWER_SLEEP_MINS` | `15` | While paused, the device wakes only this often, purely to check for a charger. |

A failed voltage read is deliberately treated as *not low* — a flaky ADC reading can never trigger the death screen.

## Advanced: calibrating the battery meter

`badge.battery_level()` is unreliable on Badgeware v2.0.2 (it only ever reports 0 or 100 — see `docs/FIRMWARE-NOTES.md`), so the header percentage is interpolated from `badge.battery_voltage()` through `_LIPO_CURVE`, a piecewise-linear single-cell LiPo discharge curve:

```python
_LIPO_CURVE = ((3.30, 0), (3.50, 10), (3.60, 20), (3.70, 40),
               (3.80, 60), (3.90, 75), (4.00, 88), (4.20, 100))
```

Cells vary. To calibrate the empty end for yours: run the badge down, note the voltage at which it actually dies (shown on the low-battery screen), and set that as the 0% point. Consider keeping `LOW_BATT_VOLTS` ~0.1 V above it.

While charging, the meter shows `chg` instead of a percentage — the charger holds the cell voltage high, so a percentage would be meaningless.

## Layout values (hardcoded but tweakable)

These aren't surfaced as constants because they're coupled to the 296×128 layout, but they're safe to nudge:

- Destination names truncate at 23 characters (`fetch_arrivals`), the weather footer's left side at 28, both with a `~` suffix.
- Row height is 16 px from y=40; columns sit at x=4 (line), x=50 (destination), right-aligned (minutes).
- Font is `rom_font.smart` throughout. Other ROM fonts exist (`ark`, `compass`, …) but metrics differ — expect to re-tune y positions, since `screen.text`'s y is the *top* of the glyphs on this firmware, not the baseline.
- The low-battery screen's grey cells use `color.smoke` under `badge.mode(FULL_UPDATE)`. Keep a non-fast update mode if you restyle it, or the grey renders near-black (see FIRMWARE-NOTES on the panel's two native greys).
