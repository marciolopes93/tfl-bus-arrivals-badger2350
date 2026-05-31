import sys
import os
import time
import socket

# Firmware 2.x: the app runtime INJECTS its API as globals — no import needed,
# exactly as the stock apps do. Injected names this app uses: screen, color,
# image, shape, rect, rom_font, badge, rtc, State, run, wait_for_button_or_alarm,
# the BUTTON_* constants and the display-mode constants.
#
# IMPORTANT: do NOT `import rtc` or `from badgeware import run, State`. `import rtc`
# returns a bare module shell that lacks set_alarm()/datetime() — a DIFFERENT
# object from the injected `rtc` instance. So we import only real stdlib modules
# here and reference everything else as injected globals.

sys.path.insert(0, "/system/apps/tfl")
os.chdir("/system/apps/tfl")

# The RTC keeps time through sleep, but the system clock (time.localtime()) may
# reset on the restart that follows each sleep. Copy RTC -> local time at startup
# so time.localtime() (used for the header clock and footer date) stays correct.
try:
    rtc.rtc_to_localtime()
except Exception:
    pass

STOP_ID   = "490003028E"
STOP_NAME = "Abbey Road"
MAX_BUSES = 6
REFRESH_MINS = 2
WEATHER_MAX_STALE_MINS = 60   # Hide weather older than this; also the retry interval

WEATHER_LOCATION = "Westminster"

# 2.x sleep model (corrected): wait_for_button_or_alarm(timeout=ms) waits for a
# button press OR an RTC alarm, then after `timeout` deep-sleeps. With NO alarm
# set it can only ever be woken by a button — so for periodic refresh we must arm
# an alarm with rtc.set_alarm() each cycle (see update()). AWAKE_MS is just how
# long to stay awake after a draw before sleeping; the alarm drives the cadence.
AWAKE_MS = 5000

TFL_URL     = "https://api.tfl.gov.uk/StopPoint/{}/Arrivals".format(STOP_ID)
WEATHER_URL = "http://wttr.in/{}?format=j1".format(WEATHER_LOCATION)

REQ_TIMEOUT = 8  # seconds

# 2.x: the old interrupt-flag dance (clear_alarm_flag / enable_alarm_interrupt
# etc.) is gone. We arm a fresh alarm with rtc.set_alarm() each cycle (in update())
# to drive the periodic wake; rtc is otherwise only used to read rtc.datetime().

buses       = []
weather     = None
error_msg   = None
last_ok_at  = None     # "HH:MM" of last successful bus fetch
weather_at  = None     # "HH:MM" of last successful weather fetch
weather_try_at = None  # "HH:MM" of last weather fetch attempt (success OR failure)
is_stale    = False    # True when last refresh failed but we have old data
initialised = False
last_refresh_minute = -1   # Minutes-since-midnight of last refresh attempt

# Persistent state — survives deep sleep / module reload
# Stored as JSON-safe data
persist = {
    "buses_json": "[]",   # JSON-serialised list of bus dicts
    "weather":    None,   # tuple as list: [condition, temp, lo, hi]
    "last_ok_at": None,
    "weather_at": None,   # "HH:MM" of last successful weather fetch
    "weather_try_at": None,  # "HH:MM" of last weather fetch attempt
}
State.load("tfl", persist)

# Hydrate the working globals from persisted state
try:
    import json
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
    """True if button A is currently pressed.

    Firmware 2.x: button polling is `badge.pressed(BUTTON_A)` (returns truthy
    when held), replacing v1's `BUTTON_A in io.pressed`. BUTTON_A is a bare
    injected name. `badge`/BUTTON_A only exist inside the run() context, but
    this is only ever called from update(), so they're available by then."""
    try:
        return badge.pressed(BUTTON_A)
    except Exception:
        return False

# ---- Network ---------------------------------------------------------------

def wifi_connected():
    """Check if WiFi is actually up right now (not just remembered state)."""
    try:
        import network
        wlan = network.WLAN(network.STA_IF)
        return wlan.isconnected()
    except Exception:
        return False

def connect_wifi():
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
    """HTTP GET with a socket-level timeout. Returns response or raises."""
    import requests
    try:
        # Try modern signature with timeout
        return requests.get(url, timeout=REQ_TIMEOUT)
    except TypeError:
        # Fallback: older firmware without timeout kwarg
        return requests.get(url)

def fetch_arrivals():
    """True on success; on failure, leaves old buses data intact."""
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
    """Persist current data so it survives deep sleep / module reload."""
    try:
        import json
        persist["buses_json"] = json.dumps(buses)
        persist["weather"]    = list(weather) if weather else None
        persist["last_ok_at"] = last_ok_at
        persist["weather_at"] = weather_at
        persist["weather_try_at"] = weather_try_at
        State.save("tfl", persist)
    except Exception:
        pass

def fetch_weather():
    """Silent failure — keeps old weather data. Records EVERY attempt (success
    or failure) so a downed service backs off instead of retrying each cycle."""
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
            weather_at = weather_try_at
        else:
            r.close()
    except Exception:
        pass
    save_state()

# ---- Refresh orchestration -------------------------------------------------

def do_refresh(force_weather=False):
    global error_msg, is_stale

    try:
        socket.setdefaulttimeout(REQ_TIMEOUT)
    except Exception:
        pass

    # If WiFi isn't up, try to (re)connect
    if not wifi_connected():
        connect_wifi()

    if not wifi_connected():
        # Couldn't get back online
        if buses:
            is_stale = True
            error_msg = None
        else:
            error_msg = "WiFi failed - press A to retry"
        return

    bus_ok = fetch_arrivals()
    # Weather is fetched at most once per WEATHER_MAX_STALE_MINS (or on manual
    # press A), so an outage backs off to hourly instead of every 2 min.
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
    """Minutes elapsed since an 'HH:MM' string, accounting for midnight."""
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
    """How many minutes since the last successful fetch (capped at 99)."""
    m = minutes_since(last_ok_at)
    return None if m is None else min(m, 99)

def weather_due():
    """Attempt a fetch only if we've never tried, or it's been at least
    WEATHER_MAX_STALE_MINS since the last attempt. This is what stops us
    hammering wttr.in every 2 min during an outage."""
    if weather_try_at is None:
        return True
    m = minutes_since(weather_try_at)
    if m is None:
        return True
    return m >= WEATHER_MAX_STALE_MINS

def weather_fresh():
    """True only if we hold weather data younger than WEATHER_MAX_STALE_MINS.
    When this is False the weather footer is removed from the display."""
    if weather is None or weather_at is None:
        return False
    m = minutes_since(weather_at)
    if m is None:
        return False
    return m < WEATHER_MAX_STALE_MINS

# ---- App lifecycle ---------------------------------------------------------

def init():
    global initialised
    initialised = False

def update():
    global initialised, last_refresh_minute

    # Work out elapsed minutes since last refresh using the RTC clock.
    # (rtc.datetime() still exists; only the alarm API was missing.)
    t = rtc.datetime()
    current_minute = t[3] * 60 + t[4]   # Minutes since midnight

    elapsed = current_minute - last_refresh_minute
    if elapsed < 0:                     # Crossed midnight
        elapsed += 24 * 60

    should_refresh = False
    manual = False

    if not initialised:
        initialised = True
        should_refresh = True
    elif elapsed >= REFRESH_MINS:
        should_refresh = True

    # 2.x: button press detection moved from `io.BUTTON_A in io.pressed`
    # to badge.pressed() (see _button_a_down()).
    if _button_a_down():
        should_refresh = True
        manual = True

    if should_refresh:
        do_refresh(force_weather=manual)
        last_refresh_minute = current_minute

    # ---- Draw ----
    screen.pen = color.white
    screen.clear()

    # Header bar
    screen.pen = color.black
    screen.rectangle(0, 0, screen.width, 18)
    screen.pen = color.white
    screen.font = rom_font.smart

    # LHS: battery %  (2.x: badge.battery_level() replaces get_battery_level())
    try:
        bat = int(badge.battery_level())
        screen.text("{}%".format(bat), 4, 3)
    except Exception:
        pass

    # Centre area: stop name (aligned with Destination column)
    title = "TFL  {}".format(STOP_NAME)
    screen.text(title, 50, 3)

    # RHS: current time
    t = time.localtime()
    ts = "{:02d}:{:02d}".format(t[3], t[4])
    w, _ = screen.measure_text(ts)
    screen.text(ts, screen.width - w - 4, 3)

    screen.pen = color.black

    if error_msg:
        screen.font = rom_font.smart
        screen.text(error_msg, 4, 40)
    else:
        # Column headers — replaced by stale message when applicable
        screen.font = rom_font.smart
        if is_stale:
            mins = minutes_since_last_ok()
            if mins is not None:
                screen.text(
                    "Stale - {} min ago - press A".format(mins),
                    4, 22
                )
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

        # Weather footer — removed entirely once data is older than an hour
        if weather_fresh():
            condition, current, lo, hi = weather
            footer_y = screen.height - 14
            screen.pen = color.black
            screen.line(0, footer_y - 4, screen.width, footer_y - 4)
            # Left: today's date + condition  (e.g. "Sat. 30/05: Partly cloudy")
            DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
            lt = time.localtime()
            date_str = "{}. {:02d}/{:02d}".format(DAYS[lt[6]], lt[2], lt[1])
            left = "{}: {}".format(date_str, condition)
            if len(left) > 28:
                left = left[:27] + "~"
            screen.text(left, 4, footer_y)
            # Right: current temp with lo/hi range
            right = "{}C ({}, {})".format(current, lo, hi)
            w, _ = screen.measure_text(right)
            screen.text(right, screen.width - w - 4, footer_y)

    # Push the e-ink only when we actually refreshed/redrew new data. On battery
    # every wake is a fresh start (should_refresh is True via the init path), so it
    # always pushes; on USB — where the badge can't deep-sleep and this loops fast
    # — it avoids redrawing the panel on every spin.
    if should_refresh:
        badge.update()

    # Arm the alarm so the device wakes in REFRESH_MINS. Order-agnostic: minutes
    # sits in the middle whether the signature is (h, m, s) or (s, m, h). Without
    # this, wait_for_button_or_alarm only ever returns on a button press.
    try:
        rtc.set_alarm(0, REFRESH_MINS, 0)
    except Exception:
        pass

    # Stay awake briefly for button A, then deep-sleep; the alarm above wakes us
    # for the periodic refresh (a button also wakes us from sleep).
    wait_for_button_or_alarm(timeout=AWAKE_MS)

def on_exit():
    pass

# 2.x migration: drop the `if __name__ == "__main__"` guard and call run()
# unconditionally at the end of the module.
run(update)
