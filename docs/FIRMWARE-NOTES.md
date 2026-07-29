# Badgeware v2.x field notes

Hard-won, on-device-verified facts about Badgeware **v2.0.2** on the Badger 2350, collected while building this app. They're documented here because the published docs and most online examples describe either the legacy "v4" BadgerOS API or behaviour that diverges from the flashed firmware. Where the two disagree, **trust the device** — `dir(rtc)`, `dir(badge)`, and live value reads at the REPL beat any documentation, and the stock apps (`gallery`, the clock) are the authoritative pattern.

Each note links to the relevant `HANDOFF.md` section for the full story.

## 1. The runtime injects its API — never import it

When an app runs, the framework injects `screen`, `color`, `image`, `shape`, `rect`, `mat3`, `vec2`, `rom_font`, `badge`, `rtc`, `State`, `run`, `wait_for_button_or_alarm`, the `BUTTON_*` constants and the display-mode constants (`FAST_UPDATE`, `MEDIUM_UPDATE`, `FULL_UPDATE`, `DITHER`, `NON_BLOCKING`) directly into the app's namespace as globals.

- `import rtc` *succeeds* but returns a different, bare module that lacks `set_alarm`, `datetime`, etc. — a confusing trap.
- `from badgeware import run, State` fails (`no module named 'badgeware.run'`) because these are builtins, not package members.
- Only real stdlib modules get imported: `sys`, `os`, `time`, `socket`, `json`, `network`, `requests`, `secrets`.

(`HANDOFF.md` §5.1, §9.1–9.2)

## 2. Deep sleep is a full reset — persist everything via `State`

`wait_for_button_or_alarm(timeout=ms)` stays awake that long, then deep-sleeps. Waking from deep sleep **reloads the module from scratch**; all globals are wiped. Anything that must survive goes through `State.load(name, dict)` / `State.save(name, dict)` (JSON-safe values only; it's `save`, not v4's `modify`).

The badge will **not** deep-sleep on USB power — there, the loop runs continuously and globals persist, so apps need both behaviours: time-based gating on USB, and persistence for battery. (§5.3)

## 3. The alarm is the only thing that wakes you — arm it defensively

Without an armed `rtc.set_alarm(...)`, `wait_for_button_or_alarm` can only ever be woken by a button. Two consequences:

- The classic "no auto-refresh, but the button works" symptom means no alarm was armed. (§9.5)
- A silently swallowed `set_alarm()` failure (`try/except: pass`) before sleeping means the device sleeps **with no wake source** and appears bricked until a button/reset. Over ~1,400 two-minute cycles the cumulative odds of one such failure caught up with this app at roughly the 48-hour mark. The fix: `rtc.clear_alarm()` at startup (a latched stale flag is a wedge candidate), then a verified retry-arm that only enters sleep once `set_alarm` succeeded, and otherwise returns so the framework re-calls `update()`. (§5.4, §9.12)
- There is **no reliable "is an alarm armed?" query** — `rtc.alarm_status()` reads the *fired* flag, not the armed state. You can only catch arming exceptions.
- `rtc.set_alarm(hours, minutes, seconds)` is a **relative delay** on v2.x, not v4's absolute wall-clock time.

A possibly cleaner mechanism, `badge.sleep(seconds)` (atomic arm+sleep), is documented in `HANDOFF.md` §12.1 with a verification checklist — proposed, not yet adopted.

## 4. `badge.battery_level()` is broken — use voltage

On v2.0.2 `battery_level()` only ever returns **100 (charging) or 0 (on battery)**, never an in-between value. `badge.battery_voltage()` is accurate (verified tracking 4.22 → 4.17 V over 90 minutes on battery). Derive a percentage from voltage through a LiPo curve, show `chg` while charging (the charger holds the cell high, so a % is meaningless), and **read the voltage before bringing WiFi up** — the ADC can read low while the radio is active on RP2350 boards. `is_charging()` and `usb_connected()` both work correctly. (§5.6, §9.10)

## 5. The RTC holds UK local time — but NTP seeds UTC

`rtc.datetime()` reads the wall clock (verified on-device during BST), so display code should use `time.localtime()` with **no timezone offset** — adding a BST offset double-counts the hour in summer. The RTC runs off the LiPo through deep sleep; a **full** battery death resets it to the 2000-01-01 epoch, and nothing in the firmware re-syncs it automatically. `rtc.time_from_ntp()` fixes that (needs WiFi up first, else `OSError -6`), but it seeds **UTC**, and there is no `set_datetime()` on this firmware to write a corrected local value back — so a death recovered during BST reads 1 hour behind until corrected. (§5.4, §9.13, §9.16)

`rtc.rtc_to_localtime()` copies the RTC into the system clock — call it at startup so `time.localtime()` is right after a wake.

## 6. The e-ink panel has two native greys — and update mode matters

The panel measures **264 × 176 px** — read from `screen.width` / `screen.height`, *not* the commonly quoted 296×128, which is a different Badger's spec and cost a two-row layout error here. `rom_font.smart` glyphs measure **6 × 12 px** (`screen.measure_text("0")`). Prefer `screen.measure_text()` over arithmetic in layout code. (§5.9)

The panel renders four levels: black, dark grey, light grey, white; any pen colour is *quantized* to the nearest. In the DawnBringer-16 palette, `color.grey` is the **dark** grey and `color.smoke` (≈ `color.rgb(160–190, …)`) is the **light** one. There is no `color.light_grey` — that was a v4 name. Crucially, greys render cleanly only under `MEDIUM_UPDATE`/`FULL_UPDATE`; the default fast update muddies them toward black, which is why naive grey fills appear solid black. Dithering is only needed for shades *beyond* the two native greys. (§5.8, §9.7, §9.15)

## 7. Drawing API changes from v4

- You must call `badge.update()` to push the framebuffer to the e-ink — nothing auto-draws. (`badge.update()` = `screen.update()` + `badge.clear()` + `badge.poll()`.)
- `screen.text`'s y coordinate is the **top-left** of the text, not the baseline. Layouts ported from v4 shift vertically.
- `screen.rectangle` is filled; draw outlines as a filled rect with a white inset.
- Buttons: `io` is gone; use `badge.pressed(BUTTON_A)` (bool with an argument, list of pressed buttons without).

## 8. Networking quirks

- HTTPS works for api.tfl.gov.uk but **fails for wttr.in** (`MBEDTLS_ERR_SSL_FATAL_ALERT_MESSAGE`) — use plain HTTP there.
- Always set a timeout: `socket.setdefaulttimeout(n)` plus `requests.get(url, timeout=n)` (with a `TypeError` fallback for firmware without the kwarg). Without one, a dead router hangs requests for minutes.
- The stock `wifi` helper's `while not wifi.tick(): pass` blocks forever when offline; a bounded manual retry against `network.WLAN` lets you give up and fall back to cached data.

## 9. Practicalities

- Two distinct "disk" modes: double-tap **RESET** for the file-editing disk (`BADGER`, rooted at `/system`); hold **BOOT** + tap **RESET** for the firmware-flashing disk (`RP2350`).
- Apps live at `/system/apps/<name>/` and must contain `__init__.py` and a file literally named `icon.png` (24×24 px B/W PNG) to appear in the launcher.
- macOS `._` metadata files on the disk are harmless.
- A handy REPL pattern for testing (the injected globals exist in the REPL environment):

  ```python
  import sys; sys.path.insert(0, "/system/apps/tfl")
  import os; os.chdir("/system/apps/tfl")
  exec(open("/system/apps/tfl/__init__.py").read())
  ```
