# Badger 2350 TFL Bus + Weather App — Project Handoff

Source-of-truth document for the TFL bus arrivals app running on a Pimoroni
Badger 2350. **Read this first before making changes.**

> **Firmware note:** This app now targets **Badgeware firmware v2.0.2**. The v2.x
> API is very different from the older BadgerOS "v4" examples this project
> started on. Everything below describes v2.x. The old v4 details have been moved
> to **§13 Legacy** and are no longer used — don't write new code against them.

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
| Display | e-ink, 296 x 128 region used (264x176 panel) |
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
  into the system clock. We call it once at startup so `time.localtime()` (used
  for the header clock and footer date) is correct after a sleep/reset.
- `rtc.time_from_ntp()` — fetches the time over the network and sets the RTC to
  **UTC**. We only call it when the RTC looks unset (`datetime()[0] < RTC_MIN_VALID_YEAR`),
  i.e. after a full battery death wiped it to the 2000 epoch (§9.13). Needs WiFi
  up first (a bare-REPL call with no connection raises `OSError -6` from inside
  `ntptime.py`). **No `set_datetime()` exists on this firmware** — `dir(rtc)`
  shows only `set_alarm`/`set_timer` as setters — so we cannot write a corrected
  local time into the RTC. We keep the RTC on UTC and apply the UK BST/GMT offset
  only at **display** time via `london_now()` (§5.7 / §9.13). Internal timing
  (staleness, `last_ok_at`, the alarm delay) stays UTC; it's all relative, so it
  needs no conversion.

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
- A flat grey fill **thresholds to solid black** on the e-ink unless you dither.
  To show a real grey, use `screen.dither()` (whole-frame) or
  `badge.mode(... | DITHER)`. *(We removed our only grey — see §7 — so the app is
  now pure black/white and needs no dithering.)*

### 5.9 Fonts
`rom_font.<name>` (injected). Available ROM pixel fonts include: `ark`, `compass`,
`futile`, `ignore`, `nope`, `sins`, `smart`, `yesterday`. **We use
`rom_font.smart`** everywhere (small, readable). v2.x fonts now support UTF-8.

---

## 6. APIs used

### TFL Unified API (buses)
- Endpoint: `https://api.tfl.gov.uk/StopPoint/{STOP_ID}/Arrivals`
- **STOP_ID = `490003028E`** (the configured stop; STOP_NAME = "Abbey Road").
- No API key required. Limits: 50 req/min unauthenticated; at a 2-min refresh we
  make ~30 req/hour. Optional future key: `api-portal.tfl.gov.uk`, append
  `?app_key=KEY`.
- Fields per arrival: `lineName`, `destinationName`, `timeToStation` (secs →
  integer-divide by 60 for minutes). Sorted ascending by minutes.

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
| 84%   TFL  Abbey Road                               08:47  |  <- header bar (black bg, white text)
|------------------------------------------------------------|
| Line   Destination                              Min        |  <- col headers (y=22)
| ---------------------------------------------------------- |  <- separator line (y=36)
| 18     Euston                                     2        |  <- rows from y=40, row_h=16
| 27     Camden Town                                5        |     plain black-on-white
| ...                                                        |
| ---------------------------------------------------------- |
| Sat. 30/05: Partly cloudy            12C (9, 18)           |  <- weather footer (removed when >1h old)
+------------------------------------------------------------+
```

- **Header**: battery meter LHS (x=4), title at x=50, current time right-aligned.
  The meter shows a voltage-derived `%` on battery, or `"chg"` while charging
  (see §5.6 / §9.10 — `battery_level()` is unusable, so we map `battery_voltage()`
  through a LiPo curve). Just the meter, no "LOW" text. The clock and footer date
  are drawn from `london_now()`, not raw `time.localtime()`: the RTC is kept on
  UTC and the UK BST/GMT offset is applied at display time only (§5.4 / §9.13).
- **Bus rows are now plain black text on white** — the old zebra/alternating-row
  shading was removed (it relied on a grey that the e-ink rendered as solid black;
  see §9). The display is clean without it, and being pure black/white means no
  dithering is needed anywhere.
- **Stale state**: the Line/Destination/Min header row is replaced by
  `Stale - {mins} min ago - press A` (capped at 99). Clears on next success.
- **Low-battery screen**: a separate full-screen state (not part of the layout
  above) — centred `LOW BATTERY` / `Updates paused.` / `Plug in USB-C to charge.`
  plus a `{%}  ({V})` line. Drawn once when paused, then left static; replaces the
  whole normal UI until charged (§8 / §9.14).
- **Weather footer**: `{Day}. {dd}/{mm}: {condition}` left (truncated to 28 chars
  with `~`), `{temp}C ({lo}, {hi})` right-aligned. The left label shows **today's
  date** from `london_now()` (a localtime-style tuple `(year, month, mday, hour,
  minute, second, weekday, yearday)`, weekday 0=Mon, via a `DAYS` tuple). The
  whole footer is
  removed once weather is older than `WEATHER_MAX_STALE_MINS`; gated by
  `weather_fresh()`. `WEATHER_LOCATION` is used only to build the fetch URL.

> Layout note: because `screen.text` y is now top-left (§5.7), if anything ever
> looks vertically off after a change, nudge the y values — it won't crash.

---

## 8. Refresh & resilience behaviour

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
    retry (§5.4 / §8). **Watchdog ruled out:** `machine.WDT` max timeout on RP2350
    is shorter than the 2-min sleep, so it would spuriously reset every normal
    cycle. **Pending on-device validation** on battery past the ~48h mark.
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
    skip it. NTP returns **UTC** and there's no `set_datetime()` to write a
    corrected local value back (only `set_alarm`/`set_timer`; confirmed via
    `dir(rtc)`), so the RTC stays UTC and the UK offset is applied at display time
    only — `london_now()` adds +1h during BST via a computed last-Sunday rule
    (no table, no hard-coded month), verified at the spring/autumn boundary hours.
    On-device check confirmed `time_from_ntp()` returns UTC: it read 10:45 when
    London was 11:45 BST. The sync is self-healing — a failed NTP attempt leaves
    the clock unset and retries next refresh.
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
    actually dies (§10).

---

## 10. Config knobs (top of `__init__.py`)

`STOP_ID`, `STOP_NAME`, `MAX_BUSES`, `REFRESH_MINS`, `WEATHER_MAX_STALE_MINS`,
`WEATHER_LOCATION`, `AWAKE_MS`, `REQ_TIMEOUT`, `RTC_MIN_VALID_YEAR` (the year
below which the RTC is treated as reset-to-epoch and an NTP re-sync is triggered;
§5.4 / §9.13), and the low-battery trio `LOW_BATT_VOLTS` / `LOW_BATT_RESUME_VOLTS`
/ `LOW_POWER_SLEEP_MINS` (the death-screen enter/resume voltages and paused poll
interval; §8 / §9.14).

Not a config-block constant but tunable: `_LIPO_CURVE` (the battery
voltage→% mapping, §5.6). To calibrate the empty end, run the cell down, note
the voltage where the badge actually dies, and set that as the `0%` point.

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
  (reliable, unlike `battery_level()`), per the idea noted here previously.
- Optional: register a TFL app key if usage ever scales.

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
