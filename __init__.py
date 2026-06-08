# TFL bus arrivals + Westminster weather — Pimoroni Badger 2350 (Badgeware v2.0.2)
#
# Full design notes, the v2.x API reference and the v4->v2 migration history live
# in HANDOFF.md; comments here cover only what that doc doesn't, plus brief
# section/flow markers. The runtime injects its API as globals (screen, color,
# rom_font, badge, rtc, State, run, wait_for_button_or_alarm, BUTTON_A) — see
# HANDOFF §5.1; do not import them.

import sys
import os
import time
import socket
import json   # at top (not lazy): needed at startup for State hydration below

sys.path.insert(0, "/system/apps/tfl")
os.chdir("/system/apps/tfl")

try:
    rtc.rtc_to_localtime()   # keep time.localtime() correct after a sleep; §5.4
    rtc.clear_alarm()        # clear any stale/latched alarm flag from the prior
                             # wake before a fresh one is armed below. A latched
                             # flag is a candidate for "alarm won't re-fire" wedges
                             # over many cycles (reliability fix).
except Exception:
    pass

# ---- Config knobs (see HANDOFF §10) ----------------------------------------

STOP_ID   = "490003028E"
STOP_NAME = "Abbey Road"
MAX_BUSES = 6
REFRESH_MINS = 2
WEATHER_MAX_STALE_MINS = 60
WEATHER_LOCATION = "Westminster"
AWAKE_MS = 400    # awake window before deep sleep. Short: a button wakes from
                  # deep sleep anyway, so this costs no responsiveness but trims
                  # the (radio-on) awake tail off every cycle — the main battery
                  # lever after REFRESH_MINS (see HANDOFF §8).
RTC_MIN_VALID_YEAR = 2024   # below this the RTC has reset to its 2000 epoch
                            # (after a full battery death) -> trigger an NTP re-sync.

# Low-battery "death screen": at/below LOW_BATT_VOLTS (and not charging) the app
# latches into a paused state — draws a static warning ONCE and stops all WiFi /
# fetches / refreshes, sleeping rarely just to poll for a charger. This both saves
# the last charge and avoids a brownout-mid-refresh blanking the panel (§9.14).
# Voltage thresholds (not %): more reliable, per HANDOFF §5.6/§10. Calibrate the
# same way as _LIPO_CURVE — note the volts where the cell actually dies.
LOW_BATT_VOLTS        = 3.45   # enter paused mode at/below this (~7-8% on the curve)
LOW_BATT_RESUME_VOLTS = 3.60   # hysteresis: auto-resume only above this (or when
                               # charging) so it can't flap at the threshold
LOW_POWER_SLEEP_MINS  = 15     # while paused, wake this rarely to poll for charge

TFL_URL     = "https://api.tfl.gov.uk/StopPoint/{}/Arrivals".format(STOP_ID)
WEATHER_URL = "http://wttr.in/{}?format=j1".format(WEATHER_LOCATION)

REQ_TIMEOUT = 8  # seconds

# ---- Runtime globals (reset on every battery wake; see HANDOFF §5.3) --------

buses       = []
weather     = None
error_msg   = None
last_ok_at  = None
weather_at  = None     # "HH:MM" of last weather SUCCESS (drives display)
weather_try_at = None  # "HH:MM" of last weather ATTEMPT (drives backoff)
is_stale    = False
initialised = False
last_refresh_minute = -1

# Persisted across deep sleep / reload (JSON-safe values only).
persist = {
    "buses_json": "[]",
    "weather":    None,
    "last_ok_at": None,
    "weather_at": None,
    "weather_try_at": None,
    "low_power":  False,   # latched low-battery pause state; survives the reload
}
State.load("tfl", persist)

# Hydrate working globals from persisted state
try:
    buses = json.loads(persist["buses_json"])
except Exception:
    buses = []
if persist["weather"] is not None:
    weather = tuple(persist["weather"])
last_ok_at = persist["last_ok_at"]
weather_at = persist.get("weather_at")
weather_try_at = persist.get("weather_try_at")

# ---- Input helpers ---------------------------------------------------------

def _button_a_down():
    """True if button A registered a press this cycle (badge.pressed; §5.5)."""
    try:
        return badge.pressed(BUTTON_A)
    except Exception:
        return False

# ---- Battery ---------------------------------------------------------------
# This firmware's badge.battery_level() is unreliable: it reports 100 while
# charging and 0 on battery, never a real in-between value. badge.battery_voltage()
# IS accurate, so we derive % from voltage via a LiPo curve instead. While
# charging the cell voltage is held high (so % is meaningless) — show "chg",
# matching the stock launcher's behaviour.

# Approx single-cell LiPo discharge curve: (volts, percent), ascending.
_LIPO_CURVE = ((3.30, 0), (3.50, 10), (3.60, 20), (3.70, 40),
               (3.80, 60), (3.90, 75), (4.00, 88), (4.20, 100))

def _pct_from_voltage(v):
    if v <= _LIPO_CURVE[0][0]:
        return 0
    if v >= _LIPO_CURVE[-1][0]:
        return 100
    for i in range(1, len(_LIPO_CURVE)):
        v0, p0 = _LIPO_CURVE[i - 1]
        v1, p1 = _LIPO_CURVE[i]
        if v <= v1:
            return int(p0 + (p1 - p0) * (v - v0) / (v1 - v0))
    return 100

def read_battery():
    """(charging, volts) read in one go. Call BEFORE WiFi comes up — the ADC can
    read low while the radio is active on RP2350 boards. `volts` is None if the
    read fails (we then never treat the battery as low — fail safe, §9.14)."""
    charging = False
    try:
        charging = badge.is_charging()
    except Exception:
        pass
    volts = None
    try:
        volts = badge.battery_voltage()
    except Exception:
        pass
    return charging, volts

def battery_str_from(charging, volts):
    """Header meter string: 'chg' while charging, else a voltage-derived %."""
    if charging:
        return "chg"
    if volts is None:
        return ""
    return "{}%".format(_pct_from_voltage(volts))

# ---- Clock / timezone ------------------------------------------------------
# The RTC runs on UK LOCAL time (confirmed on-device: rtc.datetime() reads the
# wall clock, not UTC), and keeps it off the LiPo through deep sleep. So we just
# display time.localtime() — NO display-time offset (an earlier BST offset here
# double-counted the hour in summer; §9.16). A FULL battery death still wipes the
# RTC to its 2000 epoch; sync_clock_if_needed() re-seeds it (see its note + §9.16
# for the one caveat that leaves).

def london_now():
    """UK wall-clock as a localtime-style tuple. The RTC is already on local time,
    so this is just time.localtime() — kept as a named seam in case a future
    timezone change ever needs a single place to adjust (§9.16)."""
    return time.localtime()

def sync_clock_if_needed():
    """No-op unless the RTC looks unset (year < RTC_MIN_VALID_YEAR), i.e. after a
    battery death. Then re-seed it from NTP and mirror into the system clock.
    Self-healing: a failed NTP attempt leaves the clock unset and retries next
    refresh. CAVEAT: time_from_ntp() returns UTC and there's no setter to write a
    corrected local value, so a death recovered during BST reads 1h behind until
    fixed — rare, and far better than staying stuck at the 2000 epoch (§9.16)."""
    try:
        if rtc.datetime()[0] >= RTC_MIN_VALID_YEAR:
            return
        rtc.time_from_ntp()      # seeds the RTC (UTC; see caveat above)
        rtc.rtc_to_localtime()   # mirror into time.localtime()
    except Exception:
        pass

# ---- Network ---------------------------------------------------------------
# Raw `network` is used on purpose instead of the stock `wifi` helper: the stock
# `while not wifi.tick(): pass` blocks forever when offline, whereas the bounded
# retry below gives up so do_refresh() can fall back to cached/stale data.

def wifi_connected():
    """Is WiFi actually up right now (not just remembered)?"""
    try:
        import network
        wlan = network.WLAN(network.STA_IF)
        return wlan.isconnected()
    except Exception:
        return False

def connect_wifi():
    """Bounded (re)connect: ~10s then give up."""
    try:
        import network, secrets
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        if not wlan.isconnected():
            wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
            for _ in range(20):
                if wlan.isconnected():
                    break
                time.sleep(0.5)
        return wlan.isconnected()
    except Exception:
        return False

# ---- API fetches -----------------------------------------------------------

def _get(url):
    """HTTP GET with timeout. `requests` works on this firmware; stock uses
    `urequests` if it ever breaks (TypeError fallback documented in §8)."""
    import requests
    try:
        return requests.get(url, timeout=REQ_TIMEOUT)
    except TypeError:
        return requests.get(url)

def fetch_arrivals():
    """True on success; on failure leaves old `buses` intact."""
    global buses, last_ok_at
    try:
        r = _get(TFL_URL)
        if r.status_code != 200:
            r.close()
            return False
        data = r.json()
        r.close()
        result = []
        for item in data:
            line        = item.get("lineName", "?")
            destination = item.get("destinationName", "?")
            eta_min     = item.get("timeToStation", 0) // 60
            if len(destination) > 20:
                destination = destination[:19] + "~"
            result.append({"line": line, "destination": destination, "minutes": eta_min})
        result.sort(key=lambda b: b["minutes"])
        buses = result[:MAX_BUSES]
        t = rtc.datetime()
        last_ok_at = "{:02d}:{:02d}".format(t[3], t[4])
        save_state()
        return True
    except Exception:
        return False

def save_state():
    """Persist data so it survives deep sleep / reload."""
    try:
        persist["buses_json"] = json.dumps(buses)
        persist["weather"]    = list(weather) if weather else None
        persist["last_ok_at"] = last_ok_at
        persist["weather_at"] = weather_at
        persist["weather_try_at"] = weather_try_at
        State.save("tfl", persist)
    except Exception:
        pass

def fetch_weather():
    """Silent failure (keeps old data). Stamps weather_try_at on every attempt
    for the backoff; saves on all paths. See §8."""
    global weather, weather_at, weather_try_at
    t = rtc.datetime()
    weather_try_at = "{:02d}:{:02d}".format(t[3], t[4])
    try:
        r = _get(WEATHER_URL)
        if r.status_code == 200:
            data = r.json()
            r.close()
            current_c = data["current_condition"][0]
            today     = data["weather"][0]
            condition = current_c["weatherDesc"][0]["value"]
            temp      = current_c["temp_C"]
            hi        = today["maxtempC"]
            lo        = today["mintempC"]
            weather   = (condition, temp, lo, hi)
            weather_at = weather_try_at   # mark this attempt as the last success
        else:
            r.close()
    except Exception:
        pass
    save_state()

# ---- Refresh orchestration -------------------------------------------------

def do_refresh(force_weather=False):
    """Reconnect, fetch buses, fetch weather when due/forced; degrade to
    stale/error when offline (see §8)."""
    global error_msg, is_stale

    try:
        socket.setdefaulttimeout(REQ_TIMEOUT)
    except Exception:
        pass

    if not wifi_connected():
        connect_wifi()

    if not wifi_connected():
        if buses:
            is_stale = True
            error_msg = None
        else:
            error_msg = "WiFi failed - press A to retry"
        return

    sync_clock_if_needed()   # re-sync the RTC if a battery death reset it (§9.13)

    bus_ok = fetch_arrivals()
    if force_weather or weather_due():
        fetch_weather()

    if bus_ok:
        is_stale = False
        error_msg = None
    else:
        if buses:
            is_stale = True
            error_msg = None
        else:
            error_msg = "Could not fetch - press A to retry"

def minutes_since(hhmm):
    """Minutes since an 'HH:MM' string, wrapping at midnight."""
    if not hhmm:
        return None
    try:
        hh, mm = hhmm.split(":")
        then = int(hh) * 60 + int(mm)
        t = rtc.datetime()
        now = t[3] * 60 + t[4]
        diff = now - then
        if diff < 0:
            diff += 24 * 60
        return diff
    except Exception:
        return None

def minutes_since_last_ok():
    """Minutes since last success (capped at 99)."""
    m = minutes_since(last_ok_at)
    return None if m is None else min(m, 99)

def weather_due():
    """True if a weather fetch is due (backoff; see §8)."""
    if weather_try_at is None:
        return True
    m = minutes_since(weather_try_at)
    if m is None:
        return True
    return m >= WEATHER_MAX_STALE_MINS

def weather_fresh():
    """True if weather data is younger than the stale threshold."""
    if weather is None or weather_at is None:
        return False
    m = minutes_since(weather_at)
    if m is None:
        return False
    return m < WEATHER_MAX_STALE_MINS

# ---- App lifecycle ---------------------------------------------------------
# No init(): the module reload on each battery wake already resets the globals
# above, so it's redundant (the stock apps omit init() too).

def _outline_rect(x, y, w, h, t=4):
    """Hollow rectangle: filled black, then a white inset -> a t-px black border
    (rectangle is filled on this firmware, so outlines are drawn this way)."""
    screen.pen = color.black
    screen.rectangle(x, y, w, h)
    screen.pen = color.white
    screen.rectangle(x + t, y + t, w - 2 * t, h - 2 * t)

def _ctext(s, y):
    """Horizontally-centred rom_font.smart text in black."""
    screen.font = rom_font.smart
    w, _ = screen.measure_text(s)
    screen.pen = color.black
    screen.text(s, (screen.width - w) // 2, y)

def draw_low_battery(bat_str, volts):
    """Static low-battery warning: a battery glyph with a black 'remaining'
    sliver and light-grey 'empty' cells, the reading centred inside it, and two
    lines below. Drawn ONCE when paused, then left on the panel (e-ink holds it).
    Uses FULL_UPDATE so the panel's native grey renders cleanly (color.grey is
    the DARK grey; color.smoke is the light one — see §9.15). On battery every
    wake is a fresh reload, so the mode resets itself next cycle."""
    try:
        badge.mode(FULL_UPDATE)      # two native greys need a full update; §9.15
    except Exception:
        pass
    screen.pen = color.white
    screen.clear()
    _outline_rect(42, 46, 170, 68)               # battery body
    screen.pen = color.black
    screen.rectangle(212, 66, 10, 28)            # terminal nub
    screen.rectangle(50, 54, 16, 52)             # remaining charge (black sliver)
    for x, w in ((72, 42), (118, 42), (164, 40)):
        screen.pen = color.smoke                 # empty capacity (light grey; §9.15)
        screen.rectangle(x, 54, w, 52)
    detail = bat_str or "low"
    if volts is not None:
        detail = "{}  ({:.2f}V)".format(bat_str or "low", volts)
    _ctext(detail, 72)                           # reading, centred in the battery
    _ctext("LOW BATTERY.", 124)
    _ctext("Plug in USB-C to charge.", 146)

def arm_and_sleep(mins):
    """Arm the periodic wake then deep-sleep until alarm or button. A silently
    swallowed set_alarm() failure here caused the ~48h freeze (slept with no wake
    source); retry instead of `except: pass`, clear any stale flag first, and only
    sleep once an alarm is actually armed (§5.4 / §9.12). If all retries fail we
    return so run() re-calls update() rather than freezing — busy retry, not brick."""
    armed = False
    for _ in range(3):
        try:
            rtc.clear_alarm()
            rtc.set_alarm(minutes=mins)
            armed = True
            break
        except Exception:
            time.sleep(0.2)
    if armed:
        wait_for_button_or_alarm(timeout=AWAKE_MS)

def update():
    global initialised, last_refresh_minute

    charging, volts = read_battery()    # BEFORE WiFi comes up (ADC skew); §5.6
    bat_str = battery_str_from(charging, volts)

    # ---- Low-battery gate: pause everything behind a static warning (§9.14) ----
    # Latched in persist["low_power"] so it survives the deep-sleep reload. On
    # entry we draw the warning ONCE and stop; while paused we don't redraw (the
    # panel holds the image) and skip all WiFi/fetches, waking rarely just to poll
    # for a charger. Resume only when charging or recovered above the hysteresis
    # threshold — then fall through to a normal, fresh refresh + draw.
    if persist.get("low_power"):
        if charging or (volts is not None and volts >= LOW_BATT_RESUME_VOLTS):
            persist["low_power"] = False
            save_state()
        else:
            arm_and_sleep(LOW_POWER_SLEEP_MINS)   # stay paused; no redraw
            return
    elif (not charging) and (volts is not None) and (volts <= LOW_BATT_VOLTS):
        draw_low_battery(bat_str, volts)
        badge.update()
        persist["low_power"] = True
        save_state()
        arm_and_sleep(LOW_POWER_SLEEP_MINS)
        return

    t = rtc.datetime()
    current_minute = t[3] * 60 + t[4]
    elapsed = current_minute - last_refresh_minute
    if elapsed < 0:                     # crossed midnight
        elapsed += 24 * 60

    should_refresh = False
    manual = False

    if not initialised:                 # fresh boot / battery wake -> refresh
        initialised = True
        should_refresh = True
    elif elapsed >= REFRESH_MINS:       # USB path: time-based refresh
        should_refresh = True

    if _button_a_down():                # manual: also forces a weather fetch
        should_refresh = True
        manual = True

    if should_refresh:
        do_refresh(force_weather=manual)
        last_refresh_minute = current_minute

    # ---- Draw ----
    screen.pen = color.white
    screen.clear()

    lt = london_now()                   # UK wall-clock; header clock + footer date

    # Header bar
    screen.pen = color.black
    screen.rectangle(0, 0, screen.width, 18)
    screen.pen = color.white
    screen.font = rom_font.smart
    if bat_str:
        screen.text(bat_str, 4, 3)
    screen.text("TFL  {}".format(STOP_NAME), 50, 3)
    ts = "{:02d}:{:02d}".format(lt[3], lt[4])
    w, _ = screen.measure_text(ts)
    screen.text(ts, screen.width - w - 4, 3)

    screen.pen = color.black

    if error_msg:
        screen.font = rom_font.smart
        screen.text(error_msg, 4, 40)
    else:
        screen.font = rom_font.smart
        if is_stale:
            mins = minutes_since_last_ok()
            if mins is not None:
                screen.text("Stale - {} min ago - press A".format(mins), 4, 22)
            else:
                screen.text("Stale - press A to retry", 4, 22)
        else:
            screen.text("Line", 4, 22)
            screen.text("Destination", 50, 22)
            w, _ = screen.measure_text("Min")
            screen.text("Min", screen.width - w - 4, 22)
        screen.line(0, 36, screen.width, 36)

        # Bus rows
        y = 40
        row_h = 16
        screen.pen = color.black
        for bus in buses:
            min_str = "Due" if bus["minutes"] == 0 else str(bus["minutes"])
            screen.text(bus["line"],        4,  y)
            screen.text(bus["destination"], 50, y)
            w, _ = screen.measure_text(min_str)
            screen.text(min_str, screen.width - w - 4, y)
            y += row_h

        # Weather footer (hidden once stale)
        if weather_fresh():
            condition, current, lo, hi = weather
            footer_y = screen.height - 14
            screen.pen = color.black
            screen.line(0, footer_y - 4, screen.width, footer_y - 4)
            DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
            date_str = "{}. {:02d}/{:02d}".format(DAYS[lt[6]], lt[2], lt[1])
            left = "{}: {}".format(date_str, condition)
            if len(left) > 28:
                left = left[:27] + "~"
            screen.text(left, 4, footer_y)
            right = "{}C ({}, {})".format(current, lo, hi)
            w, _ = screen.measure_text(right)
            screen.text(right, screen.width - w - 4, footer_y)

    # Push e-ink only when we refreshed (avoids constant redraw on USB; §5.3)
    if should_refresh:
        badge.update()

    arm_and_sleep(REFRESH_MINS)   # verified-retry arm + deep sleep (§5.4 / §9.12)

def on_exit():
    try:
        rtc.clear_alarm()   # don't leave a stale wake armed (matches stock apps)
    except Exception:
        pass

run(update)
