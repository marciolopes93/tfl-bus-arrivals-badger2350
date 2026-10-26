# Badger 2350 TFL Bus + Weather App — Project Handoff

Source-of-truth document for the TFL bus arrivals app running on a Pimoroni
Badger 2350. **Read this first before making changes.**

> **Firmware note:** This app now targets **Badgeware firmware v2.0.2**. The v2.x
> API is very different from the older BadgerOS "v4" examples this project
> started on. Everything below describes v2.x. The old v4 details have been moved
> to **§13 Legacy** and are no longer used — don't write new code against them.

> **Published-repo note:** `README.md` and the `docs/` folder are the curated,
> user-facing entry points (install, configuration, troubleshooting, firmware
> field notes). This file is the full development log and design source of
> truth; code comments cross-reference it by `§` section.

---

## 1. Project goal

A MicroPython app for the Pimoroni Badger 2350 e-ink badge that:

- Shows live bus arrival times for a specific London bus stop (TFL data)
- Shows a one-line weather summary for Westminster at the bottom
- Auto-refreshes on a timer, balancing freshness against battery/screen wear
- Degrades gracefully when offline (keeps last-known data with a warning)
- Shows battery level so stale data is noticeable

The app works and has been used in the real world to catch a bus. We are now
refining behaviour and polish.

---

## 2. Hardware & environment

| Item | Value |
|------|-------|
| Device | Pimoroni Badger 2350 |
| Firmware | **Badgeware v2.0.2** |
| Display | e-ink, **264 x 176** — measured on-device, not the 296x128 spec figure (§5.9) |
| Dev tool | Thonny on macOS (MicroPython / Raspberry Pi Pico interpreter) |
| Connection | USB-C; **must use a data cable, not charge-only** |
| Docs | https://badgewa.re/docs and https://github.com/pimoroni/badgeware-docs |
| Firmware repo | https://github.com/pimoroni/badger2350 (releases / changelog) |

### Two different "disk" modes (don't confuse them)
- **Disk mode (editing app files):** double-tap **RESET**. A disk appears (named
  "BADGER"/"Badger2350") whose root **is** the `/system` folder — i.e. `apps/` on
  the disk = `/system/apps/` on the device. Copy app files here.
- **Firmware flashing:** hold **BOOT**, briefly tap **RESET**. A disk named
  "RP2350" appears; drop the firmware `.uf2` onto it.

(The original v4 notes conflated these — double-tap RESET is the one for editing
files.)

### Connecting in Thonny
1. Bottom-right interpreter selector → **MicroPython (Raspberry Pi Pico)**
2. Badge plugged in, running normally (NOT in disk mode)
3. Hit the red Stop button to get the `>>>` REPL prompt
4. Port on Mac looks like `/dev/tty.usbmodem...`

After copying files: safely eject, then press RESET so the launcher rescans.
`._`-prefixed files on the disk are harmless macOS metadata; ignore them.

---

## 3. Filesystem layout

Real filesystem root contains: `rom`, `system`, `state`. Apps live at
**`/system/apps/`**. Each app is a folder:

```
/system/apps/tfl/
    __init__.py     <- the app code (MUST be named __init__.py)
    icon.png        <- launcher icon (MUST be named icon.png)
```

WiFi credentials live at **`/system/secrets.py`** (already configured) with
`WIFI_SSID` and `WIFI_PASSWORD`.

The launcher lists an app only if its folder contains a file literally named
`icon.png`, and the entry file must be `__init__.py` (matches the stock apps,
e.g. `gallery`). `/state/` holds per-app saved data written by `State` (§5).

---

## 4. The icon

`icon.png` is a **24x24 px black-and-white PNG**, a roundel based on the TFL bus
roundel. The launcher scales it up, so keep the source small (24px renders fine;
64px overflows neighbouring icons). Must be a real PNG named `icon.png`.

> v2.x note: PNGs up to 2048px wide and JPEGs are now supported by the image
> loader generally, but the 24px launcher icon is unchanged and fine as-is.

---

## 5. Badgeware v2.x API reference (the important part)

This is where almost all the migration effort went. The single biggest concept:

### 5.1 The runtime INJECTS its API — do not import it
When an app runs, the runtime injects its API straight into the app's namespace
as globals. **You do not import these, and importing them is actively wrong:**

Injected names (available at module level and inside any function): `screen`,
`color`, `image`, `shape`, `rect`, `mat3`, `vec2`, `rom_font`, `badge`, `rtc`,
`State`, `run`, `wait_for_button_or_alarm`, the `BUTTON_*` constants, and the
display-mode constants (`FAST_UPDATE`, `MEDIUM_UPDATE`, `FULL_UPDATE`, `DITHER`,
`NON_BLOCKING`).

Only **real stdlib modules** are imported: `sys`, `os`, `time`, `socket`, and
(lazily, inside functions) `json`, `network`, `requests`, `secrets`.

> ⚠️ `import rtc` does NOT give you the injected `rtc`. It returns a *different*
> bare module object that lacks `set_alarm`/`datetime`/etc. Likewise
> `from badgeware import run, State` fails ("no module named 'badgeware.run'")
> because `run` is a builtin, not a package member. **Reference all of the above
> as injected globals, never via import.** The stock `gallery` app confirms this:
> it imports only `os`, `sys`, `ui` and uses everything else injected.

### 5.2 App structure & the run/draw/sleep loop
End the module with a bare, unconditional `run(update)` (no `if __name__` guard).
`init()` / `on_exit()` are optional.

On Badger, e-ink updates are costly, so the framework does **not** auto-push the
display — you must call `badge.update()` yourself when you want the screen to
refresh. The canonical loop (matches the stock `gallery` app) is:

```python
def update():
    ...                         # decide what to do, then draw into `screen`
    if something_changed:
        badge.update()          # push the framebuffer to the e-ink
    rtc.set_alarm(0, MINS, 0)   # arm the periodic wake (see 5.4)
    wait_for_button_or_alarm(timeout=AWAKE_MS)

run(update)
```

`badge.update()` = `screen.update()` + `badge.clear()` + `badge.poll()`. Buttons
are polled automatically by the framework each cycle, so you don't poll manually.

### 5.3 Deep sleep & state persistence
`wait_for_button_or_alarm(timeout=ms)` stays awake polling for that long, then
**deep-sleeps to save battery**. Deep sleep **resets the device on wake** — the
module reloads from scratch and all globals are wiped. So persist anything that
must survive a wake via `State`:

```python
persist = { ...defaults... }
State.load("tfl", persist)   # hydrate at startup (writes loaded data into dict)
State.save("tfl", persist)   # save after changes
```

- `State.load(name, dict)` and `State.save(name, data)` — **note: it's `save`,
  not `modify`** (v4's `State.modify` no longer exists).
- Values must be JSON-safe. We store the bus list as a JSON string (`buses_json`)
  and the weather tuple as a list.
- **Badger will NOT deep-sleep while on USB power.** On USB the loop just runs
  fast and never resets, so globals persist there — which is why we still keep an
  elapsed-time refresh check *and* gate `badge.update()` on "did anything change"
  (otherwise USB would redraw the panel continuously).

### 5.4 RTC (`rtc`) — periodic wake
`rtc` is the injected instance. Relevant methods (from `dir(rtc)` on-device):
`datetime`, `set_alarm`, `clear_alarm`, `alarm_status`, `set_timer`,
`timer_elapsed`, `rtc_to_localtime`, `localtime_to_rtc`, `time_from_ntp`.

- `rtc.datetime()` → tuple `(year, month, day, hour, minute, second, dow)`. So
  `[3]`=hour, `[4]`=minute (same indices we used before — handy).
- `rtc.set_alarm(hours, minutes, seconds)` — fires after that **relative delay**
  (NOT an absolute wall-clock time like v4). We arm `rtc.set_alarm(0, REFRESH_MINS, 0)`
  each cycle. (Order-agnostic for us: minutes is the middle arg either way.)
- **Without arming an alarm, `wait_for_button_or_alarm` can only ever be woken by
  a button** — this caused the "no auto-refresh after 2 min, but button works"
  bug. The alarm is what drives the periodic wake.
- **Arm defensively (see §9.12).** A silently-swallowed `set_alarm()` failure was
  the cause of the ~48h deep-sleep freeze. The arm is now a verified retry: clear
  the flag, `set_alarm`, and on exception retry (up to 3x). We only enter
  `wait_for_button_or_alarm` once an alarm is actually armed; if all retries fail
  we return and let `run()` re-call `update()` rather than deep-sleeping with no
  wake source.
- **No reliable "is an alarm armed?" query.** `rtc.alarm_status()` reads the
  *fired* flag (it's the old v4 `read_alarm_flag`, §13), not the armed state — it
  reads False right after a successful `set_alarm`. So "verify it took" can only
  catch an *exception*; there's no clean post-arm confirmation poll.
- `rtc.clear_alarm()` — clears the alarm/flag. We call it (a) at startup, before
  arming a fresh alarm, so a stale/latched flag can't wedge the next wake — on
  battery every wake is a fresh module load, so this runs each cycle; (b) inside
  the arm retry before each `set_alarm`; and (c) in `on_exit()`.
- `rtc.rtc_to_localtime()` — copies the RTC (which keeps running through sleep)
  into the system clock. We call it at startup and after an NTP re-seed. With the
  RTC now on UTC, the system clock (`time.localtime()` with no arg) is UTC too;
  the app never displays it directly — all display goes through `london_now()`.
- `rtc.time_from_ntp()` — fetches the time over the network and sets the RTC to
  **UTC**. Called in two places: when the RTC looks unset (`datetime()[0] <
  RTC_MIN_VALID_YEAR`) after a full battery death (§9.13), and once by
  `migrate_rtc_to_utc_if_needed()` to move an old local-time install onto the UTC
  model. Needs WiFi up first (a bare-REPL call with no connection raises
  `OSError -6` from inside `ntptime.py`).
- **The RTC stores UTC; UK local time is derived at read time.** `london_now()`
  is the single seam: it reads `rtc.datetime()` (UTC), adds +1h when `_is_bst()`
  says British Summer Time, and returns a `time.localtime()`-style tuple. Every
  wall-clock consumer in the app calls it — so nothing else ever applies an
  offset, and reading `rtc.datetime()` directly (as `clock_valid()` does for the
  year) gives UTC by design. This replaced an earlier local-time-in-RTC model;
  the full reasoning, the failed intermediate attempt, and why UTC storage is the
  correct fix are in §9.16. The two payoffs: a battery-death NTP recovery is now
  exact year-round (NTP's UTC *is* the stored value — no offset to double-count),
  and the March/October clocks-change needs no re-sync because the offset is
  recomputed from the UTC instant on every read.
- **The RTC drifts, and nothing corrects drift.** Measured 2026-09-08: 1 min 40 s
  slow against NTP after roughly three months. The NTP re-sync above only fires
  when the year looks unset, never for drift. The same test confirmed the model
  on-device: `rtc.datetime()` read `17:31` while London was 18:32 (BST), and
  writing *local* time into the chip made the header read an hour fast, because
  `london_now()` then added the BST hour on top. Manual re-seed:
  `tools/set_clock.py` from the REPL (WiFi, `time_from_ntp()` with retry,
  `rtc_to_localtime()`). `ntptime` makes one UDP attempt and raises
  `OSError 110 ETIMEDOUT` when it is dropped, so any caller retries.
  `rtc.localtime_to_rtc()` works as a setter (system clock to chip), but only
  ever give it a UTC value. Periodic re-sync is an open item in §12.

### 5.5 Buttons (`badge`)
`io` is gone — it's now `badge`. Test a button with `badge.pressed(BUTTON_A)`
(returns a bool when given a button; returns the list of pressed buttons when
called with no args). Also `badge.released()`, `badge.held()`, `badge.changed()`.
- **Button A** = our manual refresh (forces an immediate bus + weather fetch).
- No software "exit to menu" is wired up; exit via the hardware Home/Reset
  buttons. (`badge.woken_by_button()` / `woken_by_reset()` exist if needed.)

### 5.6 Other `badge` features (formerly `io` / module functions)
- `badge.battery_level()` → int % (was `get_battery_level()`). **⚠️ Unreliable on
  v2.0.2: it only ever reports `100` (while charging) or `0` (on battery) — never
  a real in-between value. Do NOT use it for the meter.** Confirmed on-device:
  4.22 V on USB → `battery_level()` 100 / `is_charging()` 1; 4.17 V on battery
  (after ~90 min) → `battery_level()` 0 / `is_charging()` 0. See §9.10.
- `badge.battery_voltage()` → float volts. **Accurate** (tracked 4.22 → 4.17 V
  over 90 min). This is what the app uses: it derives % from voltage via a LiPo
  curve (`_LIPO_CURVE` / `_pct_from_voltage()`), and shows `"chg"` while charging
  (cell is held high then, so a % would be meaningless — matches stock launcher).
  Read the meter BEFORE bringing WiFi up: the ADC can read low while the radio is
  active on RP2350 boards.
- `badge.usb_connected()`, `badge.is_charging()` — both verified working (flip
  1 → 0 correctly on unplug).
- `badge.ticks` / `badge.ticks_delta` (ms), `badge.disk_free("/system")`
- `badge.mode(...)` (was `mode()`): Badger flags `FAST_UPDATE`, `MEDIUM_UPDATE`,
  `FULL_UPDATE`, `DITHER`, plus `NON_BLOCKING`. We don't set a mode currently;
  the default is fine for a 2-minute refresh.

### 5.7 Drawing (`screen`)
`screen` is an image. Raster primitives we use all still exist: `screen.pen`,
`screen.font`, `screen.clear()`, `screen.rectangle(x,y,w,h)`,
`screen.line(x0,y0,x1,y1)`, `screen.text(s, x, y)`, `screen.measure_text(s)` →
`(w, h)`. Also available: `screen.circle`, `screen.blit`, `screen.dither`,
`screen.shape(...)` for vector shapes.

- **`screen.text` y is now the TOP-LEFT corner of the text, not the baseline**
  (this changed from v4). Vertical positions behave differently as a result; the
  old "underline cutting through descenders" problem is gone because text now
  sits below its y.

### 5.8 Colour & greys
The palette is **DawnBringer-16**: `black, grape, navy, grey, brown, green, red,
taupe, blue, orange, smoke, lime, latte, cyan, yellow, white`, plus
`color.rgb(r,g,b[,a])`, `color.hsv(...)`, `color.oklch(...)`. **There is no
`color.light_grey`.**

- `color.black` / `color.white` are pure black/white on Badger.
- **The panel has two real greys** (black + 2 greys + white = 4 levels; §9.15).
  Any pen is *quantized* to the nearest of those four. `color.grey` is the **dark**
  grey; `color.smoke` (and ~`color.rgb(160-190,...)`) land on the **light** grey.
  Grey renders cleanly only under a non-fast update — we draw greys under
  `badge.mode(FULL_UPDATE)`. A fast update muddies/darkens them, which (together
  with the long-gone v4 `color.light_grey`) is why the early zebra rows and first
  grey-bar tests came out near-black. See §9.15.
- Dithering (`screen.dither()` / `badge.mode(... | DITHER)`) is only needed for
  *extra* shades beyond the two native greys — not required for a single light grey.
- Used in the low-battery screen (light-grey "empty" cells; §7 / §9.14). The main
  bus UI stays pure black/white.

### 5.9 Fonts
`rom_font.<name>` (injected). Available ROM pixel fonts include: `ark`, `compass`,
`futile`, `ignore`, `nope`, `sins`, `smart`, `yesterday`. **We use
`rom_font.smart`** everywhere (small, readable). v2.x fonts now support UTF-8.

**Measured metrics (do not trust the spec sheet).** The panel and the font were
read off the device, per the §5.1 house rule:

```python
>>> print(screen.width, screen.height, screen.measure_text("0"))
264 176 (6.0, 12.0)
```

So: **264 x 176 panel, 6 x 12 px glyphs.** The commonly quoted "296 x 128" is the
2.9" Badger spec and does not apply to this board — a §9.18 layout estimate built
on it came out wrong by two bus rows. The evidence was already in the code:
`draw_low_battery()` draws text at `y = 146`, which a 128px panel could not
render. Prefer `screen.measure_text()` over arithmetic in new layout code.

---

## 6. APIs used

### TFL Unified API (buses)
- Endpoint: `https://api.tfl.gov.uk/StopPoint/{STOP_ID}/Arrivals`
- **STOP_ID = `490003028E`** (the configured stop; STOP_NAME = "Abbey Road").
  Set this to your own stop's Naptan ID — the trailing letter distinguishes
  direction / side of road.
- No API key required. Limits: 50 req/min unauthenticated; at a 2-min refresh we
  make ~30 req/hour. Optional future key: `api-portal.tfl.gov.uk`, append
  `?app_key=KEY`.
- Fields per arrival: `lineName`, `destinationName`, `timeToStation` (secs →
  integer-divide by 60 for minutes). Sorted ascending by minutes.

### TFL Unified API (stop disruptions / closures) — §9.18
- Endpoint: `https://api.tfl.gov.uk/StopPoint/{STOP_ID}/Disruption`
- **Separate from Arrivals.** Arrivals has no idea whether the stop is open and
  will happily serve times for a closed one. This is the only place the closure
  appears.
- No API key. Returns a JSON **list** — `[]` when there is nothing wrong. Polled
  hourly (not every 2 min), so ~24 extra requests/day.
- Fields: `type` (`"Closure"` is the actionable one), `description` (prose,
  formatted for a web page — embedded newlines and indentation), `fromDate` /
  `toDate` (ISO-8601 window), `commonName`, `appearance`.
- Live example (July 2026 roadworks):
  ```json
  [{"atcoCode":"490003028E","fromDate":"2026-07-27T07:00:00Z",
    "toDate":"2026-08-09T17:00:00Z","type":"Closure","mode":"bus",
    "description":"Bus Stop Closed\n    Please use the next stop\n      or the previous stop \n       to catch your bus",
    "commonName":"Abbey Road","appearance":"Information"}]
  ```


### wttr.in (weather)
- **Use HTTP, not HTTPS** — HTTPS fails on the Badger
  (`MBEDTLS_ERR_SSL_FATAL_ALERT_MESSAGE`).
- JSON endpoint: `http://wttr.in/Westminster?format=j1` (the compact `%L`/`%H`
  format codes come back unexpanded, hence `j1`).
- Fields: `current_condition[0].weatherDesc[0].value`,
  `current_condition[0].temp_C`, `weather[0].maxtempC` / `weather[0].mintempC`.
- We format temps as e.g. `12C` and append the `C` ourselves. (v2.x fonts now
  support UTF-8, so a real `°` is possible if ever wanted — currently we don't.)

---

## 7. Current UI layout

```
+------------------------------------------------------------+
| 84%   TFL  Abbey Road                 08:47  |  <- header bar (black bg, white text)
|------------------------------------------------------------|
| Line   Destination                              Min        |  <- col headers (y=22)
| ---------------------------------------------------------- |  <- separator line (y=36)
| 18     Euston                                     2        |  <- rows from y=40, row_h=16
| 27     Camden Town                                5        |     plain black-on-white
| ...                                                        |
|############  Abbey Road Closed  #####################|  <- closure alert bar (§9.18; only while
| ---------------------------------------------------------- |     closed. Header then names the alt stop)
| Sat. 30/05: Partly cloudy            12C (9, 18)           |  <- weather footer (removed when >1h old)
+------------------------------------------------------------+
```

- **Header**: battery meter LHS (x=4), title at x=50, current time right-aligned.
  The meter shows a voltage-derived `%` on battery, or `"chg"` while charging
  (see §5.6 / §9.10 — `battery_level()` is unusable, so we map `battery_voltage()`
  through a LiPo curve). Just the meter, no "LOW" text. The clock and footer date
  are drawn from `london_now()`, which reads the UTC RTC and applies the BST
  offset at read time — the single timezone seam (§5.4 / §9.16).
- **Bus rows are now plain black text on white** — the old zebra/alternating-row
  shading was removed (it relied on a grey that the e-ink rendered as solid black;
  see §9). The display is clean without it, and being pure black/white means no
  dithering is needed anywhere.
- **Stale state**: the Line/Destination/Min header row is replaced by
  `Stale - {mins} min ago - press A` (capped at 99). Clears on next success.
- **Low-battery screen**: a separate full-screen state (not part of the layout
  above) — a battery glyph (outline body + nub) with a black "remaining" sliver
  at the left and light-grey "empty" cells (`color.smoke`, drawn under
  `FULL_UPDATE`; §9.15), the `{%}  ({V})` reading centred inside it, then
  `LOW BATTERY.` and `Plug in USB-C to charge.` below. Drawn once when paused,
  then left static; replaces the whole normal UI until charged (§8 / §9.14).
- **Quiet-hours screen** (§8 / §9.17): between `QUIET_START` and `QUIET_END` the
  bus table is replaced by a parked notice — the live header (battery/clock) and
  the weather footer around a centred message: `Buses are still running - /
  I'm asleep to conserve battery.` (one sentence wrapped over two lines — too
  wide for the panel in one), then a gap, `Auto-updates resume at 05:30.`
  (rendered from `QUIET_END`), a gap, and `Press A for an update now.`
  Repainted once per `QUIET_SLEEP_MINS` heartbeat, not per wake.
- **Closure alert bar** (§9.18): while the stop is closed, an inverted
  (white-on-black) centred bar sits on the **second-to-last line**, directly on
  top of the weather rule — and drops to the bottom line when the footer is
  hidden, so it never leaves a gap. Reads `{STOP_NAME} Closed`, naming the stop
  that is **shut**, while the header names the stop the arrivals actually came
  from. The two lines together are the whole story: what you're looking at, and
  why it changed. `ALERT_H = 18`, chosen on the panel (at 15 the text sat low with
  dead rows above it); it matches the header bar, so the two read as a pair. All
  six bus rows still fit alongside it — see the budget in §9.18.

- **Weather footer**: `{Day}. {dd}/{mm}: {condition}` left (truncated to 28 chars
  with `~`), `{temp}C ({lo}, {hi})` right-aligned. The left label shows **today's
  date** from `london_now()` (a localtime-style tuple `(year, month, mday, hour,
  minute, second, weekday, yearday)`, weekday 0=Mon, via a `DAYS` tuple). The
  whole footer is
  removed once weather is older than `WEATHER_MAX_STALE_MINS`; gated by
  `weather_fresh()`. `WEATHER_LOCATION` is used only to build the fetch URL.

> Layout note: because `screen.text` y is now top-left (§5.7), if anything ever
> looks vertically off after a change, nudge the y values — it won't crash.

> **Header text y = 2, not 3.** With an 18px bar and a 12px glyph, y=3 is
> arithmetically centred — but `rom_font.smart` renders with a pixel or two of
> built-in padding above the glyph, so 3 sits visibly low. On-device, **2** looks
> right. All three header `screen.text(...)` calls in `_draw_header()` (battery,
> title, clock) use the same value and must be changed together. `_draw_alert()`
> uses `top + 2` for the same reason, so the two bars agree.

---

## 8. Refresh & resilience behaviour

- **Stop closures (§9.18).** The disruption endpoint is polled on the same hourly
  slot as weather (and forced by a press of **A**), never on the 2-min bus cycle.
  Its failure handling is the **inverse** of weather's: a failed fetch holds the
  last known closure indefinitely and only a clean empty response clears it, so the
  device can never quietly stop warning you about a stop you can't board at. The
  retry-burst counter drives retries only, never expiry.

- **Hang watchdog (§9.19).** On battery, the chip's hardware watchdog is armed
  for 16 s at the start of each wake and fed before every slow step (WiFi join,
  each HTTP GET, each e-ink push, the sleep call). If the firmware hangs while
  awake, the chip reboots itself into the app instead of sitting powered until
  the battery dies. The first wake after a watchdog reboot only sleeps, so it can
  never loop. Deep sleep stops the watchdog, so normal 2-minute sleeps are
  unaffected. Not armed on USB.

- **REFRESH_MINS = 2.** TFL predictions are minute-precision; 2 min halves
  WiFi/display cost vs 1 min while staying useful. Press **A** for an instant
  refresh. The periodic wake is driven by an alarm armed each cycle as a verified
  retry (`rtc.clear_alarm()` + `rtc.set_alarm(minutes=REFRESH_MINS)`, retried on
  failure; §5.4 / §9.12); the device deep-sleeps between wakes on battery. A flag
  clear at startup (§5.4) prevents a stale alarm flag wedging the next wake.
- **AWAKE_MS = 400.** How long `wait_for_button_or_alarm` stays awake after a
  draw before deep-sleeping. Kept short on purpose: a button wakes the device from
  deep sleep anyway, so this window costs no responsiveness, while a long one
  (it was 2000) is pure radio-on awake-time burned every cycle. After REFRESH_MINS
  it's the main battery lever — deep sleep itself is ~free (§9.14).
- **Quiet hours (`QUIET_START`/`QUIET_END`, default 22:30–05:30; §9.17).** Inside
  the window the app parks: no bus fetching, no 2-minute wakes. It wakes once per
  `QUIET_SLEEP_MINS` (60) for a heartbeat — weather-only fetch if due, repaint of
  the quiet screen's header clock/battery and footer — and sleeps in chunks
  capped at the minutes remaining to `QUIET_END`, so the first normal refresh
  lands on time. **Press A** for `QUIET_OVERRIDE_MINS` (30) of normal live
  updates, after which it parks again if still inside the window. Disable by
  setting either bound to `None`. Skipped entirely while the RTC looks unset
  (`clock_valid()`; §9.13) so a post-death 00:00 epoch clock can't trap the
  device asleep — the normal path then runs and the NTP self-heal fixes the
  clock. ~28 fetch cycles a night replaced by ~7 heartbeats.
- **Low-battery pause (`LOW_BATT_VOLTS` / `LOW_BATT_RESUME_VOLTS` /
  `LOW_POWER_SLEEP_MINS`).** At/below `LOW_BATT_VOLTS` on battery the app latches
  into a paused "death screen": it draws a static warning once and then stops all
  WiFi/fetches/refreshes, waking only every `LOW_POWER_SLEEP_MINS` to poll for a
  charger. It resumes (to a fresh refresh) when charging or once voltage recovers
  above `LOW_BATT_RESUME_VOLTS`. The pause both preserves the last charge and
  avoids a brownout-mid-refresh blanking the panel (§9.13 / §9.14).
- **REQ_TIMEOUT = 8s.** Without a timeout, `requests.get()` can hang for minutes
  when the router is down, freezing the screen. We set `socket.setdefaulttimeout()`
  and pass `timeout=` to `requests.get` (with a `TypeError` fallback for firmware
  without the kwarg).
- **Offline flow**: each wake re-checks `wifi_connected()` live; if a fetch fails
  but we have cached `buses`, set `is_stale=True` and keep showing old data + the
  stale header. The full error screen only shows when there is NO cached data
  (e.g. first-ever launch offline). Reconnect clears stale state next refresh.
- **Weather staleness/backoff (`WEATHER_MAX_STALE_MINS = 60`)** — two persisted
  timestamps: `weather_at` (last *success*, drives display via `weather_fresh()`,
  footer hidden past 1h) and `weather_try_at` (last *attempt*, drives backoff via
  `weather_due()` so a downed wttr.in is retried at most hourly, not every cycle).
  `fetch_weather()` stamps `weather_try_at` and saves on every path so the backoff
  survives the deep-sleep reload. Weather failures are silent and never block the
  bus fetch. **Press A always force-fetches weather** (`do_refresh(force_weather=True)`).
- **Weather retry burst (`WEATHER_RETRY_BURST = 3`).** When a due (hourly)
  weather fetch fails, the next `WEATHER_RETRY_BURST` wakes each retry it
  (alongside the bus fetch, 2 min apart) before giving up — so a blip doesn't
  cost the footer for an hour. The footer is **held on screen through the
  burst** (`weather_fresh()` honours it) and only drops once the burst is
  exhausted without a success; attempts then return to hourly. Driven by a
  persisted consecutive-failure counter (`weather_fails`, reset on any success,
  capped at 99 so it can't grow unbounded). Nuance: during quiet hours wakes are
  hourly, so a burst there spreads across hours — harmless, but it means the
  footer can linger a few hours longer overnight during a wttr.in outage.

---

## 9. Migration learnings & bugs hit (v4 → v2.0.2)

The hard-won specifics, so we don't repeat them:

1. **`ImportError: no module named 'badgeware.run'`** — `run` is a builtin, not a
   package member. Don't import it; call `run(update)` bare. Same lesson applies
   to `State` and the rest: they're injected, not imported (§5.1).
2. **`AttributeError: 'module' object has no attribute 'clear_alarm_flag'` / `set_alarm`**
   — caused by `import rtc`, which returns a bare module *different* from the
   injected `rtc` instance. Drop the import and use the injected `rtc`.
3. **The old RTC interrupt dance is gone** — no `clear_alarm_flag`,
   `enable_alarm_interrupt`, etc. Just `rtc.set_alarm(...)`.
4. **`set_alarm` is a relative delay**, args `(hours, minutes, seconds)` — not the
   v4 absolute-time signature.
5. **No auto-refresh, but button works** — because no alarm was armed.
   `wait_for_button_or_alarm` only returns on a button unless an `rtc.set_alarm`
   is set to wake it. Arm it each cycle.
6. **`State.modify` doesn't exist** → use `State.save`.
7. **`color.light_grey` doesn't exist** (palette is DawnBringer-16). A plain grey
   fill thresholds to solid black on the e-ink unless dithered — which is why the
   zebra rows came out black-on-black. We removed the striping entirely (§7).
8. **`screen.text` y is now top-left, not baseline** — layout shifts vertically.
9. **`badge.battery_level()`** replaces `get_battery_level()`; `io` → `badge`;
   button polling is `badge.pressed(BUTTON_A)`. (But `battery_level()` itself is
   unusable on this firmware — see item 10.)
10. **`badge.battery_level()` reports 0/100 only — meter showed a wrong value.**
    Symptom: the header stuck at 100% even when unplugged. On-device readings
    (via a temporary debug strip) showed `battery_level()` returns 100 while
    charging and 0 on battery, with nothing in between, while `battery_voltage()`
    tracked the real cell (4.22 V on USB → 4.17 V after ~90 min on battery).
    `is_charging()`/`usb_connected()` were fine (flipped 1 → 0 on unplug). Fix:
    derive % from `battery_voltage()` via `_LIPO_CURVE`/`_pct_from_voltage()`, and
    show `"chg"` while charging instead of a number. Read the meter before WiFi
    comes up (radio activity can skew the ADC). Two pages found while debugging
    (`badger.github.io/hack/battery-percentage` and the launcher's `menu/ui.py`)
    document the *legacy v4* API (`get_battery_level()`/`is_charging()` as free
    imports, `screen.brush`/`shapes`/`io.ticks`) — useful for the "chg" idea, but
    do NOT copy their calls; they don't exist on v2.0.2 (§13).
11. **Meta-lesson:** the published docs can lag/diverge from the flashed firmware
    (the docs listed `rtc` methods the *imported* module lacked; the battery
    pages above are for a different API entirely). When in doubt, trust the device
    itself — `dir(rtc)`, `dir(badge)`, and live value reads at the REPL or via an
    on-screen debug strip — and the stock apps (`gallery`, clock) as the
    authoritative pattern for your build.
12. **~48h deep-sleep freeze — device dark until manual reset.** Symptom: ran fine
    for ~48h (~1,400 wake cycles at REFRESH_MINS=2), then dropped off the network
    and froze on its last drawn frame (e-ink retains the image with no power); a
    reset + reopen recovered it instantly, battery was fine (~31%). Root cause: the
    arm was fire-and-forget —
    ```python
    try:
        rtc.set_alarm(minutes=REFRESH_MINS)
    except Exception:
        pass
    wait_for_button_or_alarm(timeout=AWAKE_MS)
    ```
    If `set_alarm()` ever failed on a cycle, the `except: pass` swallowed it and
    the device deep-slept with **no wake source** — only a button/reset could
    revive it. Over ~1,400 cycles the cumulative odds of one such failure add up
    (statistical, not deterministic — hence ~48h, not a fixed time). Contributing
    factor: the alarm flag was never cleared across wakes (v1.0.0 cleared it at
    load; v2.x only cleared in `on_exit()`, which doesn't run across a deep-sleep
    reset). Fix (two parts): (a) `rtc.clear_alarm()` at startup so each fresh wake
    starts from a clean flag; (b) replace the silent arm with a verified retry that
    only deep-sleeps once an alarm is armed, and otherwise returns to let `run()`
    retry (§5.4 / §8). **Correction (2026-10-04):** an earlier version of this
    entry ruled a watchdog out, because `machine.WDT`'s ~16.7 s maximum is
    shorter than the 2-min sleep. That was wrong: deep sleep powers the chip off,
    which stops the watchdog, so it only counts while the app is awake. A
    watchdog would not have caught *this* freeze (the chip was off, with no wake
    source), but it does catch the different, awake-and-hung freeze in §9.19.
    **Pending on-device validation** on battery past the ~48h mark.
13. **Full battery death resets the clock to 2000-01-01 (Sat 00:00).** Symptom:
    after the LiPo fully died, the screen came back blank (a normal brownout — the
    panel was mid-`update()` after `screen.clear()`, unlike the §9.12 freeze which
    retained its last frame because power stayed on), and on reopening, the header
    clock read `00:00` and the footer `Sat 01/01`. Both are one event: the RTC
    runs off the LiPo and reverts to its 2000-01-01 epoch when the cell dies, and
    1 Jan 2000 was a Saturday. **The device does NOT self-sync the clock from the
    net** — whatever set it originally doesn't re-run; our app only ever called
    `rtc.rtc_to_localtime()` (RTC→system copy), never a fetch. Fix: call
    `rtc.time_from_ntp()` (the unused method from §5.4) once WiFi is up, gated on
    the RTC year looking unset (`< RTC_MIN_VALID_YEAR`) so normal deep-sleep wakes
    skip it. The sync is self-healing — a failed NTP attempt leaves the clock unset
    and retries next refresh. **Resolved (see §9.16):** the app now stores **UTC** in
    the RTC and derives UK local time at read time in `london_now()`. Because NTP's
    seed is UTC — exactly the stored model — the recovery is correct year-round; the
    earlier "1h behind during BST after a recovery" wrinkle no longer exists.
14. **Low-battery pause ("death screen").** To stop the device draining its last
    charge into doomed refreshes (and to avoid the §9.13 brownout-mid-refresh
    blanking), the app latches into a paused state at/below `LOW_BATT_VOLTS` on
    battery: it draws a static centred warning ONCE, then skips all WiFi/fetches
    and does not redraw (e-ink holds the image with no power), waking only every
    `LOW_POWER_SLEEP_MINS` to poll for a charger. The state is latched in
    `persist["low_power"]` so it survives the deep-sleep reload — that's what lets
    "draw once, then stop" work across wakes. Resume is gated with hysteresis
    (charging, or voltage ≥ `LOW_BATT_RESUME_VOLTS`) so it can't flap at the
    threshold, and on resume it falls through to a normal fresh refresh. Design
    notes: thresholds are **voltage**, not `battery_level()` (§9.10); a failed
    voltage read (`None`) is treated as NOT low (fail-safe — never paints the
    death screen on a bad ADC read); `is_charging()` drives resume independently
    of the voltage read, so plugging in always recovers even if the ADC is flaky.
    The arm/sleep tail was refactored into `arm_and_sleep(mins)` so both the
    normal (`REFRESH_MINS`) and paused (`LOW_POWER_SLEEP_MINS`) paths share the
    §9.12 verified-retry arm. **Pending on-device validation:** run the cell down
    to confirm the warning paints before collapse and that plugging in resumes
    cleanly; calibrate `LOW_BATT_VOLTS` against the voltage where the badge
    actually dies (§10). **Visual:** the screen is a battery glyph — outline body
    + nub, a black "remaining" sliver, light-grey "empty" cells, the reading
    centred inside, `LOW BATTERY.` / `Plug in USB-C to charge.` below. The grey
    cells use `color.smoke` under `FULL_UPDATE` (§9.15); `draw_low_battery()` sets
    that mode itself. Safe because this screen only paints on battery, where every
    wake is a fresh reload that resets the mode — the main UI keeps the fast default.
15. **The panel has two native greys — earlier "grey = black" was wrong cause.**
    When adding grey bars to the low-battery screen, `color.grey` and every
    `badge.mode()` / dither combination we tried came out near-black, seemingly
    confirming §5.8's old "grey thresholds to black." Reading the official
    Badgeware docs corrected this: the Badger displays **black, white and two
    greys** (4 levels), and any pen is *quantized* to the nearest level; update
    speed affects how greys render. Two things had compounded: (a) `color.grey`
    is the **dark** grey in DawnBringer-16, so it quantized to the dark level (or
    black under a fast update) — the light grey is `color.smoke`, or
    `color.rgb(160–190, …)` (all three looked identical on-device); and (b) the
    app's default fast update muddies greys, so they only render cleanly under
    `MEDIUM_UPDATE`/`FULL_UPDATE`. The original v1.0.0 zebra grey worked because it
    used v4's `color.light_grey`, which the v2.x migration deleted (§9.7 / §13) —
    so that good grey was never reproducible by name under v2.x. Fix: draw the
    low-battery grey cells with `color.smoke` under `badge.mode(FULL_UPDATE)`. No
    self-dithering needed (a dither only buys *extra* shades beyond the two native
    greys). Verified on-device via a swatch test (`grey`/`taupe`/`smoke`/`latte`
    plus `rgb(120/160/190/220)`): `smoke` ≈ `rgb160` ≈ `rgb190` gave the wanted
    light grey. Lesson reinforced (§9.11): the device + the *official* source beat
    a guessed pile of modes — should have read the colour docs before chasing
    `mode()` flags.
16. **Timezone: RTC stores UTC, UK local is derived at read time.** This entry has
    a two-step history; the current design is at the bottom.

    *Attempt A (regression).* The §9.13 clock work assumed the RTC was UTC and added
    a UK BST offset at display time. It read `Mon 00:13` when it was really `Sun
    23:13` BST — the offset double-counted, because the RTC at that time was holding
    **local** wall-clock (`rtc.datetime()` → `(2026, 6, 7, 23, 20, 21, 6)` while
    London was 23:20). That offset was dropped.

    *Attempt B (local-only).* `london_now()` became just `time.localtime()`, no
    offset — correct while the RTC held local time. But it left a genuine hole:
    (a) a full battery death re-seeds from NTP, which returns **UTC**, and with no
    `set_datetime()` on this firmware to write a corrected local value back, a
    summer recovery read 1h behind until fixed by hand; and (b) nothing handled the
    twice-yearly clocks-change during normal running — after the October switch a
    still-alive badge would run an hour fast until a death or a manual fix, because
    the app only ever re-syncs on the death path.

    *Current design (fixes both).* **Store UTC in the RTC; convert to UK local at
    read time.** `london_now()` reads `rtc.datetime()` (now UTC), adds +1h when
    `_is_bst()` says British Summer Time, and returns a `time.localtime()`-style
    tuple; every wall-clock consumer goes through it. `_is_bst()` decides from the
    **UTC** instant — +1h from 01:00 UTC on the last Sunday of March to 01:00 UTC on
    the last Sunday of October — using `_last_sunday()` (weekday of the 31st), so no
    hard-coded calendar. This is coherent where Attempt A was not: Attempt A applied
    an offset *on top of* an RTC that already held local time (double count); here
    the RTC holds UTC, so exactly one offset is applied, in one place. Both holes
    close: an NTP recovery is exact year-round (NTP's UTC *is* the stored value), and
    the March/October switch needs no re-sync because the offset is recomputed each
    read. **Migration:** `migrate_rtc_to_utc_if_needed()` re-seeds the RTC from NTP
    once on the first WiFi-up refresh of this build and latches
    `persist["utc_migrated"]`, moving an old local-time install onto the UTC model.
    Lesson retained from Attempt A (§9.11): the fix is
    correct *because* it's designed around what the RTC actually holds — now made
    unambiguous by writing UTC ourselves rather than inferring it.
17. **Quiet hours (overnight pause) — design decisions.** Implemented per §8;
    the non-obvious choices, for when this needs touching:
    - **Gate order:** low battery first (safety wins), then quiet hours, then the
      normal refresh path.
    - **Override latch:** persisted as `persist["quiet_override_at"]` ("HH:MM" of
      the A press) because every battery wake is a fresh reload (§5.3). It is
      explicitly cleared both on expiry and whenever a wake lands *outside* the
      window — otherwise, since `minutes_since()` wraps at 24h, a stale stamp
      from last night would read as recent at the same time tomorrow night and
      spuriously re-trigger the override.
    - **Button across deep sleep:** a quick tap that wakes the device can be
      released before the reloaded module's first `badge.pressed()` poll, so the
      override trigger is `badge.pressed(BUTTON_A) or badge.woken_by_button()`.
      **`woken_by_button()` is UNVERIFIED in this role** — if it ever reports
      True on *alarm* wakes, the override re-arms every wake and never expires
      (quiet hours silently defeated). Validation item in §12; the fallback is
      to drop it from the OR and accept that a quick tap may need a second press.
    - **Chunked sleeps, not one long alarm:** 60-min `set_alarm` chunks rather
      than a single alarm to `QUIET_END` — multi-hour alarms are unexercised on
      this firmware (§9.11), a single failed long arm is the §9.12 failure mode
      writ large, and the chunks provide the hourly heartbeat for free. Each
      chunk is `min(QUIET_SLEEP_MINS, mins_until_quiet_end())` so the wake at
      the window's end is exact.
    - **USB:** the loop spins instead of deep-sleeping (§5.3), so the heartbeat
      repaint/fetch is gated by `last_quiet_draw_minute` — a plain global that
      persists on USB but resets to -1 on every battery wake, which is what
      makes each battery heartbeat repaint unconditionally. It's also reset to
      -1 when the override expires so the quiet screen repaints over the
      override's bus table on USB.
    - **Heartbeat weather fetch** goes direct (`connect_wifi()` +
      `fetch_weather()`), not via `do_refresh()` — no bus fetch, no stale/error
      state changes overnight. The socket default timeout is therefore now also
      set once at module load, since this path skips `do_refresh()`'s.
    **Validated on-device (USB, Thonny):** the parked screen draws once and
    holds (no redraw loop on the spinning USB loop); a press of A enters the
    override (live table, `persist["quiet_override_at"]` stamped) and it expires
    cleanly back to the parked screen (latch cleared to None); the normal
    daytime table is unaffected; `woken_by_button` exists on `badge` and reads
    False in a USB session; the window math passes its checks on the device's
    MicroPython. **Still pending — battery only (§12):** `woken_by_button()`
    semantics on alarm vs button wakes; a 60-minute `set_alarm` chunk firing;
    one full unattended night.

---

### 9.18 TFL publishes stop closures on a *different* endpoint (+ failover)

**Symptom.** During roadworks near the stop the countdown unit at the home stop
showed a fault, the TFL website showed live arrivals *under a closure banner*,
and the Badger showed the same arrivals with **no banner at all** — the one
consumer of the three that was silently wrong. Times for a stop
no bus will pull into are worse than no times, because they read as actionable.

**Cause.** `/StopPoint/{id}/Arrivals` has no notion of whether the stop is open.
The closure lives on a separate endpoint, `/StopPoint/{id}/Disruption` (§6), which
nothing in the app was calling.

**Fix.** Poll the disruption endpoint on the **hourly weather cadence** — closures
change on a scale of days, so ~24 extra GETs/day against ~720 bus fetches is noise
against `REFRESH_MINS`, the actual battery lever (§8). When an active `Closure` is
reported, redirect `fetch_arrivals()` to `ALT_STOP_ID`, retitle the header, and
draw the alert bar (§7). A successful empty response puts everything back.

`active_stop()` is the single source of truth for the failover — arrivals, the
header and the alert text all read it, so they cannot disagree about which stop is
on the panel.

#### The fail-safe runs the OPPOSITE way to weather

Weather degrades gracefully: a failed fetch eventually drops the footer, because a
missing footer is honest and a stale one isn't (§8). **Closure state must not
degrade.** A failed fetch *holds* the last known closure indefinitely; only a
successful `[]` clears it. The cost of wrongly holding is a walk to a stop that
reopened; the cost of wrongly dropping is standing at a closed one. Asymmetric, so
the code is asymmetric.

`disruption_fails` therefore drives **only the retry burst** (re-fetch every wake
while `1..DISRUPTION_RETRY_BURST`), never expiry — the mirror image of
`weather_fails`, which does drive expiry. `disruption` and `disruption_type` are
written on the success path *only*.

#### ISO dates compare correctly as plain strings

`fromDate` / `toDate` are ISO-8601, and ISO-8601 sorts lexically. So the window
check is a string compare of the `YYYY-MM-DD` prefix against a formatted
`rtc.datetime()` — no date parsing on MicroPython at all:

```python
if len(frm) >= 10 and today < frm[:10]: return False
if len(to)  >= 10 and today > to[:10]:  return False
```

`_window_active()` **fails open** in every ambiguous case. Critically it returns
`True` when `clock_valid()` is False: after a full battery death the RTC reads its
2000 epoch, which would compare as "before every `fromDate`" and silently suppress
a live closure — the §9.13 failure mode leaking into a new subsystem. Same
reasoning as the quiet-hours clock guard (§9.17).

#### The bar went below the table, and costs nothing

It sits on the second-to-last line rather than in the y=22 status slot, so the
Line/Destination/Min headings survive and the arrivals still read as a table. The
y=22 slot therefore keeps its original precedence untouched — `Stale …` > headings
(§7) — and the two messages never compete. They say different things: *don't go
there* vs *don't trust these numbers*.

Vertical budget with a closure showing, on the real 264x176 panel (§5.9):

| Element | y | Height |
|---|---|---|
| header bar | 0 | 18 |
| heading row (y=22) + rule (y=36) | 18 | 22 |
| 6 bus rows @ 16px | 40 | 96 (ends 136) |
| *slack* | 136 | 4 |
| alert bar | 140 | 18 |
| weather rule + footer | 158 | 18 (ends 176) |

Everything fits with 4px to spare, so **no bus rows are lost**. Horizontally
there's more room still: at 6px/char `Abbey Road Closed` is ~100px in a 264px
bar, and any longer name is `_fit()`-truncated to fit.

> **Correction.** The first draft of this section claimed the bar cost 2–3 bus
> rows. That was derived from an assumed 296x128 panel and was wrong — see §5.9.
> Measure, don't calculate.

`_rows_bottom()` remains as a **safety net**, not a design constraint: it never
fires at the current geometry. It exists so a future `MAX_BUSES` bump, a taller
`ALERT_H` or a wider font degrades by dropping the last row rather than by drawing
bus times underneath a black bar. It reads `screen.measure_text("0")` for row
height rather than assuming one. It only clamps while the bar is up, so the normal
screen is byte-for-byte the layout it always was.

#### Wording: the bar names the closed stop, the header names the live one

`alert_text()` returns `"Abbey Road Closed"` while the header reads
`TFL  Buckingham Palace Rd`. Putting the alt stop's name in both would have said the same
thing twice and left the closure unexplained.

`_fit()` is applied to the **name only**, against the width left after `" Closed"`
is measured. Trimming the whole string risks a cut like `Long Stop Name Clos~`,
losing the one word that matters.

TFL's own `description` is deliberately **not** shown on a closure: at 6px/char it
is 456px against a 264px panel, and its advice ("use the next stop or the previous
stop") is already spent — the panel is showing that stop. Non-closure notices are
rarer and unmodelled, so those pass TFL's text straight through, `_fit()`-truncated.

#### Choosing the alternate stop

`ALT_STOP_ID` should be a nearby stop on the same route and direction you can
walk to. Prefer the **next** stop downstream: it's usually closest, and because
its arrival times are *later* than the home stop's, the walk is already priced
into the number on screen — walking to the *previous* stop inverts that, leaving
you chasing a time that's already elapsing. Confirm your chosen alt is itself
clear (a `[]` from its own `/Disruption`) before relying on it.

**Trap:** the two directions of one physical stop pair have Naptan IDs that
differ only in the trailing letter and share a parent `490G…` code. The
opposite-direction ID is **not** a usable neighbour — don't let a lookup that
resolves via the parent stop hand you the wrong side of the road.

#### Behaviour matrix

| Situation | Arrivals from | Header | y=22 row | Alert bar |
|---|---|---|---|---|
| Normal | `STOP_ID` | Abbey Road | Line / Destination / Min | — |
| Active closure | `ALT_STOP_ID` | Buckingham Palace Rd | Line / Destination / Min | `Abbey Road Closed` |
| Closure + stale buses | `ALT_STOP_ID` | Buckingham Palace Rd | `Stale - 7 min ago - press A` | `Abbey Road Closed` |
| Closure, fetch failing | `ALT_STOP_ID` (held) | Buckingham Palace Rd | as above | held until a clean `[]` |
| Closure, no arrivals at all | — | Buckingham Palace Rd | `Could not fetch` at y=40 | still drawn |
| Non-closure notice | `STOP_ID` | Abbey Road | Line / Destination / Min | TFL's text, truncated |
| `ALT_STOP_ID = None` | `STOP_ID` | Abbey Road | Line / Destination / Min | `Abbey Road Closed` |
| Quiet hours | not fetched | per `active_stop()` | — | not drawn |
| Post battery death | `ALT_STOP_ID` if reported | — | — | shown; window check fails open |

Quiet hours deliberately don't surface the alert: the heartbeat doesn't fetch
disruption, so anything drawn could be a night old, and normal refreshes resume at
`QUIET_END` regardless (§9.17).

#### Ordering inside `do_refresh()`

Disruption is fetched **before** arrivals. Running it second would leave the panel
one cycle behind the closure — showing the closed stop's times under a "closed"
bar.

#### Verification

**Validated on-device:** all six bus rows render alongside the bar and the weather
line; `ALERT_H = 18` centres the text; `Abbey Road Closed` renders in full,
centred, no truncation; the panel and font metrics were read from the REPL (§5.9).

**Still pending (§12):** ghosting when the bar clears (only observable at the end
of a real closure); the automatic return to `STOP_ID` when the closure expires;
whether the extra hourly GET pushes the awake window past `AWAKE_MS`.

---

### 9.19 Weekly daytime freeze with a draining battery (+ hang watchdog)

**Symptom.** Roughly once a week, always in the daytime, the panel froze on the
bus table. Button A did nothing, and the battery drained fast until the badge
died, sometimes from above 60%. A reset brought it back.

**Not the §9.12 freeze.** That one was the chip asleep with no wake source:
dark, but drawing almost no power. A fast drain means the opposite here. The chip
is awake and powered, but the app's code has stopped running. The app is what
polls button A, so A is dead too.

**Cause (diagnosed from the firmware source, not caught in the act).** On
Badgeware v2.0.2, the deep-sleep call `powman.sleep()` hard-asserts if the
chip's power-off request is refused. The board's panic handler then loops
forever with the chip powered. It is rare, in the order of one in a few
thousand sleeps. In the daytime the app sleeps every 2 minutes, about 500 times
a day, so once a week fits. Overnight quiet hours (§9.17) sleep hourly, which
fits it never happening at night. A hang inside the network stack would look
the same. Neither path returns to Python, so no `try/except` can catch it.
Firmware v3.1.1 still has the same sleep code, so an upgrade alone would not fix
it.

**Fix: the chip's hardware watchdog.** A 16 s timer that reboots the chip unless
the app keeps resetting ("feeding") it.

- **Armed once per wake, battery only** (`_wdt_start()`), at `WDT_MS = 16000`,
  falling back to 8000 if the chip refuses. Not armed on USB: there the
  framework's wait loop spins unfed and never sleeps, so it would reboot every
  cycle under Thonny.
- **Fed** (`_feed()`) at the start of `update()`, during the WiFi join loop,
  before each HTTP GET, before each e-ink push and just before the sleep call.
  A normal wake never comes close to 16 s between feeds.
- **Deep sleep powers the chip off, which stops the watchdog**, so the 2-minute
  sleeps are unaffected and it costs no battery.
- **Recovery.** If the chip hangs, the watchdog reboots it. A watchdog reboot is
  not a RESET press, so the launcher is expected to resume this app rather than
  the menu. Inferred from the launcher code, not yet observed on the badge.
- **Boot-loop guard** (`_skip_after_watchdog()`). The first wake after a watchdog
  reboot does no network work and draws nothing: it arms the alarm and sleeps
  for `REFRESH_MINS`. A persisted latch (`persist["wdt_skip"]`) alternates, so
  it can never skip two wakes in a row, and a persistently broken network costs
  at most one attempt per two cycles, never a reboot loop with the radio on.
  The only visible sign of a recovery is the panel missing one refresh.
  The guard relies on `badge.wake_reason()` returning `powman.WAKE_WATCHDOG`
  after a watchdog reboot. That is also inferred, not observed. If it is wrong,
  the guard never triggers and every wake after a reboot runs a normal refresh.
- **Plugging in USB mid-wake** while the watchdog is armed forces one reboot.
  Harmless.

**Verification.** Compiled clean with `mpy-cross`. A simulation harness ran a
normal wake, three back-to-back watchdog wakes, a normal wake after them and a
USB session: the watchdog armed only on battery, fed at every step, and the
skip latch alternated as designed. Installed on the badge 2026-10-04 (still
firmware v2.0.2). It then ran to 2026-10-26, about three weeks, with no frozen
screen and no unexplained battery death, against a previous rate of about one a
week. The app does not log watchdog resets, so whether the watchdog actually
fired in that period is unknown; the evidence is that the freeze stopped.

**Keep it after any firmware upgrade.** It does not stop the firmware glitch; it
makes the app recover from it, and v3.1.1 has the same glitch.

---


## 10. Config knobs (top of `__init__.py`)

`STOP_ID`, `STOP_NAME`, `MAX_BUSES`, `REFRESH_MINS`, `WEATHER_MAX_STALE_MINS`,
`WEATHER_RETRY_BURST` (extra per-wake retries after a failed hourly weather
fetch before the footer drops; §8), `WEATHER_LOCATION`, `AWAKE_MS`, `REQ_TIMEOUT`,
`RTC_MIN_VALID_YEAR` (the year
below which the RTC is treated as reset-to-epoch and an NTP re-sync is triggered;
§5.4 / §9.13), the low-battery trio `LOW_BATT_VOLTS` / `LOW_BATT_RESUME_VOLTS`
/ `LOW_POWER_SLEEP_MINS` (the death-screen enter/resume voltages and paused poll
interval; §8 / §9.14), and the quiet-hours quartet `QUIET_START` / `QUIET_END`
("HH:MM" strings; set either to `None` to disable) / `QUIET_SLEEP_MINS`
(heartbeat + chunk length) / `QUIET_OVERRIDE_MINS` (press-A live window; §8 /
§9.17).

The stop-closure set (§9.18): `ALT_STOP_ID` (the stop arrivals fall over to while
closed; set to `None` for notice-only, no failover) / `ALT_STOP_NAME` (header
title while failed over — keep it short, it shares the bar with the clock and
battery) / `DISRUPTION_MAX_STALE_MINS` (hourly re-check cadence) /
`DISRUPTION_RETRY_BURST` (extra per-wake retries after a failed fetch — drives
retries **only**, never expiry, unlike its weather counterpart).

Layout constants near the drawing helpers rather than in the config block:
`ALERT_H` (alert bar height, 18 — set on the panel, not by arithmetic; §9.18),
`FOOTER_RULE_Y` (distance from the panel bottom to the weather rule, 18 — equal to
`ALERT_H` by coincidence, named so a future tweak doesn't conflate them) and
`ALERT_SUFFIX` (`" Closed"`, appended to `STOP_NAME` and never truncated).


Also tweakable in code (not config-block constants): destination names truncate
at 23 characters in `fetch_arrivals()` (`[:22] + "~"`; widened from 20), and the
footer's left label at 28.

`WDT_MS` (hang-watchdog timeout, 16000; §9.19) sits with the watchdog code
rather than in the config block. Don't raise it: the RP2350 maximum is ~16.7 s.

Not a config-block constant but tunable: `_LIPO_CURVE` (the battery
voltage→% mapping, §5.6). To calibrate the empty end, run the cell down, note
the voltage where the badge actually dies, and set that as the `0%` point.

The low-battery screen's grey is `color.smoke` drawn under `badge.mode(FULL_UPDATE)`
(both set inside `draw_low_battery()`; §9.15). To restyle it, swap the pen
(`color.rgb(160–190, …)` is equivalent) — but keep a non-fast update or the grey
darkens.

---

## 11. Conventions / preferences

- Test changes in Thonny first via:
  ```python
  import sys; sys.path.insert(0, "/system/apps/tfl")
  import os; os.chdir("/system/apps/tfl")
  exec(open("/system/apps/tfl/__init__.py").read())
  ```
  (Easier to read errors and paste them back than debugging on-device.) Note the
  injected globals (`screen`, `rtc`, `badge`, …) must exist in the REPL session
  for this to run — they do in the normal app/REPL environment.
- Keep the font as `rom_font.smart`.
- Keep the UI minimal and pure black/white: no redundant status flags, no greys;
  shorten messages that overflow the screen width.

---

## 12. Open ideas / possible next steps

- **Validate the deep-sleep freeze fix on battery (§9.12).** The arm is now a
  verified retry + startup flag-clear; confirm the device survives past the ~48h
  mark where it previously froze. The USB dev session never deep-sleeps, so this
  alarm-wake path is still the lightly-exercised one — leave it on battery to
  exercise it (the ~31% reading at the last freeze leaves ample runway to test).
- **Validate quiet hours on battery (§9.17).** USB-side behaviour is verified
  (parked screen draw-once-and-hold, press-A override entry and expiry, daytime
  table unaffected). Three battery checks remain: (a) `badge.woken_by_button()`
  reads False on alarm wakes and True on button wakes
  (if it misreports, the press-A override never expires — drop it from the
  trigger OR as the fallback); (b) a 60-minute `rtc.set_alarm` chunk actually
  fires (only ≤2-min alarms are battle-tested so far); (c) one full unattended
  night, confirming the parked screen holds, the hourly heartbeat repaints, and
  live updates resume at `QUIET_END`.
- **Finish validating the closure failover on-device (§9.18).** Layout and
  wording are confirmed (six rows fit, `ALERT_H = 18`, no truncation). Three
  checks remain: (a) **ghosting** — the alert bar appears and disappears
  mid-session over a region that otherwise holds bus rows; watch the transition
  when it clears and force `FULL_UPDATE` if it ghosts (the §9.15 pattern);
  (b) the **automatic return** to `STOP_ID` when the closure expires — confirm the
  header and arrivals switch back on the *same* cycle, and that the reclaimed rows
  redraw rather than leaving the bar's footprint; (c) whether the extra hourly GET
  pushes the awake window past `AWAKE_MS`.
- **Also check the alt stop's own disruption (§9.18).** If both stops close, the
  app fails over into a second closed stop, silently. One more GET on the same
  hourly slot; untested against a real double-closure, so not built.
- **Auto-resolve the neighbouring stop** from `/Line/{id}/Route/Sequence` instead
  of a hardcoded `ALT_STOP_ID`. Considered and rejected: the sequence payload is
  large for this much RAM and the answer changes roughly never. Noted in case the
  stop is ever reconfigured.
- **Sweep for stale `296x128` references.** The panel is 264x176 (§5.9). Fixed in
  §2, §5.9 and §9.18; check `README.md` and `docs/` too.

- **Outage backoff (future release).** During a WiFi outage every 2-min wake
  burns ~10 s of radio in `connect_wifi()` — the most expensive failure mode
  left. Persist a consecutive-failure counter and stretch the sleep (e.g. 5 then
  10 min) until a fetch succeeds; clears on reconnection.
- **`wlan.active(False)` before sleeping (future release).** Probably moot —
  deep sleep should power the radio down anyway — but it's a one-line REPL check
  (compare sleep current with/without) and a one-line add if it helps.
- If battery life needs work, consider `badge.mode(...)` tuning.
- **Migrate the periodic wake to `badge.sleep()` (the cleaner fix).** Detailed
  proposal in **§12.1** below — a structural replacement for the arm+wait pair
  that would supersede the §9.12 retry. Deferred until verified on-device.
- Now that fonts support UTF-8, optionally show a real `°` instead of `C`.
- Split `WEATHER_MAX_STALE_MINS` into separate refresh-interval vs stale-threshold
  knobs if more-frequent healthy weather refresh is wanted.
- **DONE — low-battery pause behaviour (§8 / §9.14).** Implemented as a latched
  "death screen": at/below `LOW_BATT_VOLTS` on battery the app draws a static
  warning once and stops all work until charged. Triggers off a voltage threshold
  (reliable, unlike `battery_level()`), per the idea noted here previously. The
  screen is a battery glyph with a black "remaining" sliver, light-grey "empty"
  cells (`color.smoke` under `FULL_UPDATE`; §9.15) and the reading centred inside.
- Optional: register a TFL app key if usage ever scales.
- **DONE — weekly daytime freeze (§9.19).** Hardware watchdog with a boot-loop
  guard, on battery only. Installed 2026-10-04; no freeze in the three weeks to
  2026-10-26. Keep it through any firmware upgrade.
- **Periodic NTP re-sync for clock drift (§5.4).** The RTC ran 1 min 40 s slow
  over ~3 months, and the app only re-syncs after a battery death. A once-a-day
  `time_from_ntp()` (with retry) on the hourly weather slot would fix it.
  `tools/set_clock.py` is the manual fix meanwhile.
- **Firmware v3.1.1 upgrade (separate job).** Not done alongside the watchdog so
  that the watchdog's test stayed clean. It needs app changes (font API, text
  rendering, a device filesystem reset) and keeps the §9.19 glitch, so the
  watchdog stays.
- **DONE — death-recovery clock during BST (§9.16).** Resolved by storing UTC in
  the RTC and deriving UK local time at read time in `london_now()`, so NTP's UTC
  seed needs no local-write path at all. Also removed the twice-yearly
  clocks-change gap (the offset is recomputed each read). A one-time
  `migrate_rtc_to_utc_if_needed()` re-seeds old local-time installs. **Pending
  on-device confirmation:** watch the first live BST↔GMT switch (next: last Sunday
  of October) and the next battery-death recovery land correct without a manual fix.

### 12.1 Proposed cleaner wake mechanism: `badge.sleep()` (deferred)

**Status:** proposed, NOT yet implemented or verified on v2.0.2. The current
shipped mechanism is the §9.12 verified-retry arm. This is the cleaner replacement
to migrate to *after* on-device verification.

**Why.** The current wake is two separate steps at the end of `update()`: arm the
alarm (`rtc.set_alarm`), then sleep (`wait_for_button_or_alarm`). The §9.12 freeze
existed precisely *because* they're separate — if arming fails, the sleep still
runs and the device sleeps with no wake source. The §9.12 fix makes that gap
*safe* (retry; only sleep once armed; otherwise return and let `run()` retry), but
the gap is still structurally present and guarded by defensive code.

`badge.sleep(seconds)` is documented as a single, indivisible call that arms a
timer, deep-sleeps, and wakes on timer **or** button. There's no gap between
arming and sleeping, so the §9.12 failure mode cannot occur by construction —
a structural fix rather than a defensive one.

**The swap.** Replace the §9.12 arm-retry + `wait_for_button_or_alarm` block at
the end of `update()`:

```python
# CURRENT (§9.12 verified-retry arm)
armed = False
for _ in range(3):
    try:
        rtc.clear_alarm()
        rtc.set_alarm(minutes=REFRESH_MINS)
        armed = True
        break
    except Exception:
        time.sleep(0.2)
if armed:
    wait_for_button_or_alarm(timeout=AWAKE_MS)
```

with:

```python
# PROPOSED (atomic sleep)
badge.sleep(REFRESH_MINS * 60)
```

**What it supersedes / removes if adopted:**
- The 3x arm-retry loop and the `armed` guard (§9.12) — no longer needed.
- `AWAKE_MS` becomes unused on the battery path (no separate awake window before
  sleeping); check whether the USB path still needs it (see verification below).
- Possibly the startup `rtc.clear_alarm()` — keep it initially as cheap insurance;
  only remove once confident `badge.sleep()` manages its own flag.
- The `on_exit()` `rtc.clear_alarm()` can stay (harmless, matches stock apps).

**Verification checklist (do ALL on the actual badge before adopting — docs
diverge from firmware on this device, §9.11):**
1. **Exists & doesn't raise:** `dir(badge)` shows `sleep`; calling it doesn't
   throw on v2.0.2.
2. **Timer wake:** on battery, the device wakes and refreshes after ~REFRESH_MINS
   (not just on a button).
3. **Button wake:** a button press still wakes it early from the sleep (so manual
   refresh / responsiveness is preserved).
4. **USB behaviour:** on USB (where the badge won't truly deep-sleep), confirm the
   call returns and the loop keeps running rather than blocking or busy-spinning —
   this is the riskiest case. If it blocks/misbehaves on USB, either keep a
   USB-power branch using the old arm+wait, or gate `badge.sleep()` on
   `not badge.usb_connected()`.
5. **Survives the ~48h mark** on battery, same as the §9.12 validation.

**Rollback:** if any check fails, revert to the §9.12 block (it's the proven
mechanism). Keep both documented here until `badge.sleep()` has cleared all five.

---

## 13. Legacy — old BadgerOS "v4" (no longer used)

We started this project on the older v4-era BadgerOS API and have fully migrated
off it. Kept here only so old code/notes are intelligible. **Do not write new
code against this.**

### v4 → v2.x quick mapping
| v4 (old) | v2.0.2 (current) |
|----------|------------------|
| `from badgeware import run, rtc, get_battery_level, State` | none — all injected globals (§5.1) |
| `if __name__ == "__main__": run(update)` | bare `run(update)` |
| `io.BUTTON_A in io.pressed` | `badge.pressed(BUTTON_A)` |
| `get_battery_level()` | `badge.battery_level()` **but it reads 0/100 only — use `battery_voltage()` + curve; see §5.6/§9.10** |
| `mode(...)` | `badge.mode(...)` |
| `rtc.clear_alarm_flag()` / `enable_alarm_interrupt(True)` (at module load) | gone — not needed |
| `rtc.set_alarm(seconds, minutes, hours)` (absolute) | `rtc.set_alarm(hours, minutes, seconds)` (relative delay) |
| `rtc.read_alarm_flag()` | `rtc.alarm_status()` (but we don't read it) |
| `State.modify(name, dict)` | `State.save(name, dict)` |
| framework auto-draws; don't call `screen.update()` | must call `badge.update()` to push e-ink |
| `screen.text` y = baseline | `screen.text` y = top-left |
| `color.light_grey` | gone; use `color.grey`/`color.smoke`/`color.rgb` + dither |
| no working software exit | still none; use hardware buttons |

### v4-era issues (historical, now irrelevant)
The v4 build had its own crop of problems we solved at the time — wrong app path
(`/apps` vs `/system/apps`), `ImportError: badger2350` (it was `badgeware`),
`'image' object has no attribute 'update'`, `run.exit()` not existing, the alarm
never firing because `read_alarm_flag()` was cleared before `update()`, and deep
sleep wiping globals (fixed with `State`). These are all moot under v2.x, which
reorganised the relevant APIs entirely (see §9 for the v2.x equivalents).

---

## 14. Current file

The working app is a single file: `__init__.py` (deployed to
`/system/apps/tfl/__init__.py`), plus `icon.png` (24x24) in the same folder. The
latest version of both accompanies this handoff.
