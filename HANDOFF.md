# Badger 2350 TFL Bus + Weather App — Project Handoff

This is the source-of-truth document for the TFL bus arrivals app running on a
Pimoroni Badger 2350. Read this first before making changes.

---

## 1. Project goal

A MicroPython app for the Pimoroni Badger 2350 e-ink badge that:

- Shows live bus arrival times for a specific London bus stop (TFL data)
- Shows a one-line weather summary for Westminster at the bottom
- Auto-refreshes on a timer, balancing data freshness against battery/screen wear
- Degrades gracefully when offline (keeps showing last-known data with a warning)
- Shows battery level so stale data is noticeable

The app is already working and has been used in the real world to catch a bus.
We are now refining behaviour and polish.

---

## 2. Hardware & environment

| Item | Value |
|------|-------|
| Device | Pimoroni Badger 2350 |
| Firmware | BadgerOS **v4.03** |
| Display | e-ink, `screen.width` x `screen.height` (296 x 128 region used) |
| Dev tool | Thonny on macOS (MicroPython / Raspberry Pi Pico interpreter) |
| Connection | USB-C; **must use a data cable, not charge-only** |

### Getting into USB disk mode
With the badge back-side up: hold **BOOT** (far left), then briefly tap **RESET**.
A disk appears. In disk mode the disk root **is** the `/system` folder
(i.e. what you see as `apps/` on the disk = `/system/apps/` on the device).

### Connecting in Thonny
1. Bottom-right interpreter selector → **MicroPython (Raspberry Pi Pico)**
2. Badge plugged in, NOT in disk mode (running normally)
3. Hit the red Stop button to get the `>>>` REPL prompt
4. Port on Mac looks like `/dev/tty.usbmodem...`

---

## 3. Filesystem layout (IMPORTANT)

The real filesystem root contains only: `rom`, `system`, `state`.

Apps live at **`/system/apps/`**, NOT `/apps/`. Each app is a folder:

```
/system/apps/tfl/
    __init__.py     <- the app code (MUST be named __init__.py)
    icon.png        <- launcher icon (MUST be named icon.png)
```

WiFi credentials live at **`/system/secrets.py`** (already configured on device)
with `WIFI_SSID` and `WIFI_PASSWORD`.

### Launcher rules (learned from reading the menu app source)
The menu (`/system/apps/menu/app.py`) only lists an app if BOTH are true:
1. The path is a directory
2. It contains a file literally named **`icon.png`**

The app's entry file MUST be **`__init__.py`** (matches the convention of the
built-in `badge` app). Naming it `tfl.py` will NOT work.

`._`-prefixed files on the disk are harmless macOS metadata; ignore them.

After copying files: safely eject, then press RESET to make the launcher rescan.

---

## 4. The icon

`icon.png` is a **24x24 px black-and-white PNG**, a roundel (circle with a
horizontal bar) based on the TFL bus roundel. BadgerOS scales the launcher icon
x2 for display, so keep the source small; a 64px source renders too big and
overflows neighbouring icons. 24px renders fine.

> Note: make sure the deployed file is a real PNG named `icon.png` — a JPEG
> renamed to `.png` may not decode on the badge.

---

## 5. BadgerOS v4 API reference (learned by inspecting built-in apps)

This was the single biggest source of friction — the v4 API differs a lot from
older Badger MicroPython examples online. Key facts:

### App structure
Apps define `init()`, `update()`, `on_exit()` and end with:
```python
if __name__ == "__main__":
    run(update)
```
`run()` injects globals into the app's namespace — you do NOT import these:
`screen`, `io`, `color`, `image`, `brush`, `shape`, `mat3`, `vec2`, `rom_font`.

### Importable from `badgeware`
`run`, `rtc`, `get_battery_level`, `State`, `clamp`, `is_dir`, `file_exists`,
`SpriteSheet`, plus more.

### Drawing (`screen`)
- `screen.pen = color.white` / `color.black` / `color.light_grey`
- `screen.clear()`, `screen.rectangle(x, y, w, h)`, `screen.line(x1,y1,x2,y2)`
- `screen.text(string, x, y)` — **y is the text baseline**, not the top. This is
  why an underline drawn too close to the header text cut through descenders;
  give a few px clearance below text baselines.
- `screen.measure_text(string)` → `(w, h)`; used for right-aligning.
- `screen.font = rom_font.smart` (the small readable font we settled on)
- `screen.width`, `screen.height`

### Fonts
ROM fonts live in `/rom/fonts/*.ppf`. Accessed as `rom_font.<name>`
(e.g. `rom_font.smart`, `rom_font.ignore`, `rom_font.ziplock`). We tried
`rom_font.ignore` (bigger) but it looked "funky"; **we use `rom_font.smart`
everywhere**. `rom_font` only exists inside the `run()` context.

### Buttons (`io`)
`if io.BUTTON_A in io.pressed:` — also BUTTON_B, BUTTON_C, BUTTON_UP, BUTTON_DOWN.
- **Button A** = our manual refresh.
- **There is no working software "exit to menu"** for our case. Exit is via the
  hardware Home/Reset buttons on the back. We removed all BUTTON_C / exit code.
  (Returning a path string from `update()` like the menu does was unreliable
  here.)

### RTC (`rtc`) — periodic wakeups
This is how periodic refresh works on e-ink (there is no tight frame loop).
The clock app pattern:
```python
# At module load — REQUIRED or alarms never fire:
rtc.clear_alarm_flag()
rtc.clear_timer_flag()
rtc.enable_alarm_interrupt(True)
rtc.enable_timer_interrupt(True)
# To schedule a wake:
rtc.set_alarm(seconds, minutes, hours)
```
- `rtc.datetime()` → tuple; indices used: `[3]`=hours, `[4]`=minutes.
- **Do NOT rely on `rtc.read_alarm_flag()` inside `update()`** to detect the
  wake — the framework appears to clear it before `update()` runs, so that
  branch never fired (this was a real bug). Instead we always re-arm the alarm
  and compute elapsed time from the RTC clock.

### State persistence (`State`) — survives deep sleep
The device deep-sleeps between wakeups, which **reloads the module from scratch
and wipes all globals**. This caused the "WiFi failed" bug (empty `buses` after
reload made it take the no-data path instead of the stale path). Fix, per the
clock app:
```python
persist = { ... defaults ... }
State.load("tfl", persist)     # hydrate at startup
State.modify("tfl", persist)   # save after changes
```
Values must be JSON-safe. We store the bus list as a JSON string
(`buses_json`) and the weather tuple as a list.

---

## 6. APIs used

### TFL Unified API (buses)
- Endpoint: `https://api.tfl.gov.uk/StopPoint/{STOP_ID}/Arrivals`
- **STOP_ID = `490003028E`** (the configured stop; NaptanId from the stop's URL
  `https://tfl.gov.uk/bus/stop/490003028E/abbey-road/`). STOP_NAME shown = "Abbey Road".
- No API key required. Rate limits: 50 req/min unauthenticated, 500 req/min with
  a free key. At a 2-min refresh we make ~30 req/hour — nowhere near limits.
- Optional future key: register at `api-portal.tfl.gov.uk`, append `?app_key=KEY`.
- Fields used per arrival: `lineName`, `destinationName`, `timeToStation` (secs;
  we integer-divide by 60 for minutes). Sorted ascending by minutes.

### wttr.in (weather)
- **Must use HTTP, not HTTPS** — HTTPS fails on the Badger with
  `MBEDTLS_ERR_SSL_FATAL_ALERT_MESSAGE`.
- We use the JSON endpoint: `http://wttr.in/Westminster?format=j1`
  - The compact `?format=...` codes `%L`/`%H` (hi/lo) are NOT expanded — they
    come back as literal "%L"/"%H". That's why we switched to `j1` JSON.
- Fields used:
  - `current_condition[0].weatherDesc[0].value` → condition text
  - `current_condition[0].temp_C` → current temp
  - `weather[0].maxtempC` / `weather[0].mintempC` → today hi / lo
- The degree symbol `°` arrives as UTF-8 bytes that render as `Â°` on e-ink, so
  we never display `°` — we format temps as e.g. `12C` and append C ourselves.

---

## 7. Current UI layout

```
+------------------------------------------------------------+
| 84%   TFL  Abbey Road                               08:47  |  <- header bar (black)
|------------------------------------------------------------|
| Line   Destination                              Min        |  <- col headers (y=22)
| ---------------------------------------------------------- |  <- line at y=36
| 18     Euston                                     2        |  <- rows from y=40,
| 27     Camden Town                                5        |     row_h=16, zebra
| ...                                                        |     shading on even rows
| ---------------------------------------------------------- |
| Sat. 30/05: Partly cloudy            12C (9, 18)           |  <- weather footer
+------------------------------------------------------------+               (removed when weather > 1h old)
```

- **Header**: battery `%` on LHS (x=4), title at x=50 (aligned with Destination
  column), current time right-aligned. No "LOW" battery text — just the %.
- **When data is stale**: the Line/Destination/Min header row is replaced by
  `Stale - {mins} min ago - press A` (minutes capped at 99). No other stale
  flags anywhere. This clears automatically on the next successful refresh.
- **Weather footer**: `{Day}. {dd}/{mm}: {condition}` on the left (e.g.
  `Sat. 30/05: Partly cloudy`; truncated to 28 chars with `~` if needed),
  `{temp}C ({lo}, {hi})` right-aligned. The left label shows **today's date**,
  not the location — the date comes from `time.localtime()` (tuple
  `(year, month, mday, hour, minute, second, weekday, yearday)`, `weekday` 0=Mon),
  using a `DAYS` tuple for the weekday abbreviation. We use `time.localtime()`
  here (same as the header clock) rather than `rtc.datetime()` because its tuple
  layout is the documented/reliable one for the date fields. `WEATHER_LOCATION`
  is still used to build the wttr.in fetch URL — it's just no longer shown in the
  footer. The entire footer (separator line + both text lines) is **removed**
  once the weather data is older than `WEATHER_MAX_STALE_MINS` (1h) — we never
  show stale weather. It reappears on the next successful fetch. Gated by
  `weather_fresh()`.

---

## 8. Refresh & resilience behaviour

- **REFRESH_MINS = 2.** Rationale: TFL predictions are minute-precision and bus
  ETAs rarely change faster than ~60-90s; 2 min halves WiFi/display cost vs 1 min
  while staying useful. Press **A** anytime for an instant refresh when you
  actually need accuracy at the stop. Tunable via `REFRESH_MINS`.
- **REQ_TIMEOUT = 8s.** Without a timeout, `requests.get()` hangs for minutes
  when the router is down, freezing the screen (this was a real bug — symptom was
  the clock not advancing for 5+ min with no warning). We set both
  `socket.setdefaulttimeout()` and pass `timeout=` to `requests.get` (with a
  `TypeError` fallback for firmware without the kwarg).
- **Offline flow** (e.g. router down 5 min): on each wake, `wifi_connected()` is
  re-checked live; if a fetch fails but we have cached `buses`, we set
  `is_stale=True` and keep showing old data + the stale header. Full error screen
  (`... press A to retry`) only shows when there is NO cached data at all (e.g.
  first-ever launch offline). On reconnect, next refresh clears stale state.
- **Weather refresh & staleness (`WEATHER_MAX_STALE_MINS = 60`).** Weather is
  decoupled from the bus cadence and tracked with TWO persisted timestamps:
  - `weather_at` — last *successful* fetch. Drives the display: `weather_fresh()`
    is true only if data is younger than 1h, else the footer is removed.
  - `weather_try_at` — last *attempt* (success OR failure). Drives the retry
    backoff: `weather_due()` only attempts a fetch if it's been ≥1h since the
    last attempt. `fetch_weather()` stamps this and calls `save_state()` on every
    path (including failures), so the backoff survives the deep-sleep module
    reload — otherwise globals reset and we'd retry every wake.
  - Net: healthy → weather refreshes ~hourly; outage → footer disappears after
    1h and we retry only ~hourly (NOT every 2 min). **Press A always force-fetches
    weather** regardless of backoff (`do_refresh(force_weather=True)`).
  - Weather failures are still silent and never block the bus fetch.
  - Note: backoff interval and display-staleness threshold are intentionally the
    same 60-min knob. To refresh more often when healthy but still back off during
    outages, split into two constants.

---

## 9. Bugs we hit and fixed (so we don't repeat them)

1. **App not in launcher** — files were in `/apps/tfl` (doesn't exist); correct
   path is `/system/apps/tfl`.
2. **Still not in launcher** — entry file must be `__init__.py`, and an
   `icon.png` (that exact name) must be present.
3. **`ImportError: badger2350`** — wrong module; v4 uses `badgeware`,
   `requests` (not `urequests`), and the injected globals.
4. **`'image' object has no attribute 'update'`** — don't call `screen.update()`;
   the `run()` loop handles drawing.
5. **`'function' object has no attribute 'exit'`** — `run.exit()` isn't a thing;
   removed exit logic, use hardware buttons.
6. **Underline cutting through header text** — `screen.text` y is the baseline;
   move separator lines down a few px.
7. **Auto-refresh never firing** — relying on `rtc.read_alarm_flag()` in
   `update()`; framework clears it first. Switched to elapsed-time check via RTC.
8. **Alarms never waking device** — must call `rtc.enable_alarm_interrupt(True)`
   (+ clear flags) at module load.
9. **"WiFi failed" instead of stale message** — deep sleep wiped globals; fixed
   with `State` persistence so `buses` survives reloads.
10. **wttr.in HTTPS fails** — use HTTP.
11. **`%L`/`%H` not expanding** — use the `j1` JSON endpoint instead.
12. **`Â°` garbage / `2~` truncation** — strip the degree symbol and reformat
    temps; move current temp to the right side so it isn't truncated.
13. **Stale weather shown for hours + retried every 2 min during a wttr.in
    outage** — when the service went down, the frozen temp kept displaying (no
    weather equivalent of the bus stale flag) and `weather_due()` (then keyed off
    last *success*) stayed true, hammering the site every cycle. Fixed by keying
    display off `weather_at` (hide past 1h) and backoff off `weather_try_at`
    (attempt at most hourly), persisting both. See §8.

---

## 10. Conventions / preferences established

- Test changes in Thonny first via:
  ```python
  import sys; sys.path.insert(0, "/system/apps/tfl")
  import os; os.chdir("/system/apps/tfl")
  exec(open("/system/apps/tfl/__init__.py").read())
  ```
  (Easier to read errors and paste them back than debugging on-device.)
- Keep the font as `rom_font.smart`; the bigger font looked off.
- Keep the UI minimal: no redundant status flags; messages must fit the screen
  width (we shorten when they overflow).
- Config knobs live at the top of `__init__.py`: `STOP_ID`, `STOP_NAME`,
  `MAX_BUSES`, `REFRESH_MINS`, `WEATHER_MAX_STALE_MINS`, `WEATHER_LOCATION`,
  `REQ_TIMEOUT`.

---

## 11. Open ideas / possible next steps

- Verify the deep-sleep + `State` behaviour over a long idle period (hours).
- Consider showing a tiny "last updated" time even when NOT stale (optional).
- Consider a low-battery threshold behaviour (currently we only show the %).
- Worst-case unresponsiveness during back-to-back fetch timeouts (bus + weather
  ≈ up to 16s): now reduced — weather only fetches ~hourly, so most cycles are
  bus-only (~8s). Still worth confirming the 16s cycles are acceptable.
- Possible: split `WEATHER_MAX_STALE_MINS` into separate refresh-interval vs
  stale-threshold knobs if more-frequent healthy weather refresh is wanted.
- Optional: register a TFL app key if usage ever scales.

---

## 12. Current file

The working app is a single file: `__init__.py` (deployed to
`/system/apps/tfl/__init__.py`), plus `icon.png` (24x24) in the same folder.
The latest version of both accompanies this handoff.
