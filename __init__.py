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
import machine
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
WEATHER_RETRY_BURST = 3    # after a due (hourly) weather fetch fails, retry this
                           # many more times — one per wake, alongside the bus
                           # fetch — before the footer drops and attempts fall
                           # back to hourly. Footer is held through the burst (§8).
WEATHER_LOCATION = "Westminster"
AWAKE_MS = 400    # awake window before deep sleep. Short: a button wakes from
                  # deep sleep anyway, so this costs no responsiveness but trims
                  # the (radio-on) awake tail off every cycle — the main battery
                  # lever after REFRESH_MINS (see HANDOFF §8).
RTC_MIN_VALID_YEAR = 2024   # below this the RTC has reset to its 2000 epoch
                            # (after a full battery death) -> trigger an NTP re-sync.

# Stop disruption / closure failover (§9.18). TfL publishes stop-level closures on
# a SEPARATE endpoint from arrivals — /StopPoint/{id}/Arrivals keeps serving live
# times for a stop nobody can board at, which is exactly the trap this closes. The
# endpoint returns a JSON list ([] when nothing is wrong), needs no API key, and
# changes on a scale of days, so it rides the hourly weather cadence rather than
# the 2-minute bus one (~24 extra GETs/day against ~720 — negligible; §8).
#
# When an ACTIVE closure is reported at STOP_ID the app transparently switches the
# arrivals fetch to ALT_STOP_ID and retitles the header, so the panel shows times
# you can actually act on. Set ALT_STOP_ID = None to keep the notice but stay on
# STOP_ID (notice-only mode).
#
# Pick ALT_STOP_ID as a nearby stop on the same route and direction that you can
# realistically walk to. Prefer the NEXT stop downstream: it's usually close by,
# and because its arrival times are later than your home stop's, the walk is
# already priced into the number on screen (walking to the PREVIOUS stop inverts
# that — you'd be chasing a time that's already elapsing). The values below are
# illustrative placeholders; set your own from the TfL Naptan IDs of your stops.
ALT_STOP_ID   = "490004555P"
ALT_STOP_NAME = "Buckingham Palace Rd"  # keep short: shares the header bar with the
                                        # clock and battery meter (~20 chars max)
DISRUPTION_MAX_STALE_MINS = 60
DISRUPTION_RETRY_BURST    = 3   # same burst shape as weather, but the FAIL-SAFE
                                # RUNS THE OTHER WAY: a failed fetch HOLDS the last
                                # known closure indefinitely. Only a successful
                                # empty response clears it. Never silently drop a
                                # warning that stops you boarding (§9.18).

# Quiet hours (§8 / §9.17): inside this window the app parks — no bus fetching,
# no 2-minute wakes — behind a static notice. It wakes once per QUIET_SLEEP_MINS
# for a heartbeat (refresh weather if due; repaint header clock/battery and the
# footer), sleeping in chunks capped at the time remaining to QUIET_END so the
# first normal refresh lands on time. Press A for QUIET_OVERRIDE_MINS of normal
# live updates. Set EITHER bound to None to disable entirely. Skipped while the
# RTC looks unset (post battery death) so a wrong clock can't trap the device.
QUIET_START = "22:30"
QUIET_END   = "05:30"
QUIET_SLEEP_MINS    = 60
QUIET_OVERRIDE_MINS = 30

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

WEATHER_URL = "http://wttr.in/{}?format=j1".format(WEATHER_LOCATION)
DISRUPTION_URL = "https://api.tfl.gov.uk/StopPoint/{}/Disruption".format(STOP_ID)

def arrivals_url(stop_id):
    """Arrivals endpoint for a stop. Built per-call rather than as a constant
    because the stop in use can change under failover (§9.18)."""
    return "https://api.tfl.gov.uk/StopPoint/{}/Arrivals".format(stop_id)

REQ_TIMEOUT = 8  # seconds

try:
    # Default socket timeout for ALL fetch paths. do_refresh() repeats this
    # (harmless), but the quiet-hours heartbeat fetch (§9.17) doesn't go
    # through do_refresh, so it's set once here too.
    socket.setdefaulttimeout(REQ_TIMEOUT)
except Exception:
    pass

# ---- Runtime globals (reset on every battery wake; see HANDOFF §5.3) --------

buses       = []
weather     = None
error_msg   = None
last_ok_at  = None
weather_at  = None     # "HH:MM" of last weather SUCCESS (drives display)
weather_try_at = None  # "HH:MM" of last weather ATTEMPT (drives backoff)
weather_fails = 0      # consecutive failed weather attempts (drives the burst; §8)
disruption      = None # normalised one-line TfL notice, or None for "all clear"
disruption_type = None # TfL `type` field — "Closure" is the one that triggers
                       # failover; anything else is shown but not acted on
disruption_at     = None  # "HH:MM" of last disruption SUCCESS
disruption_try_at = None  # "HH:MM" of last disruption ATTEMPT (drives backoff)
disruption_fails  = 0
is_stale    = False
initialised = False
last_refresh_minute = -1
last_quiet_draw_minute = -1   # quiet-hours heartbeat gate; matters on USB where
                              # the loop spins instead of deep-sleeping (§9.17)

# Persisted across deep sleep / reload (JSON-safe values only).
persist = {
    "buses_json": "[]",
    "weather":    None,
    "last_ok_at": None,
    "weather_at": None,
    "weather_try_at": None,
    "weather_fails": 0,        # survives the reload so a burst spans wakes (§8)
    "disruption":      None,   # the closure must survive the deep-sleep reload,
    "disruption_type": None,   # or every wake would show an unwarned stop for
    "disruption_at":     None, # up to an hour until the next hourly fetch (§9.18)
    "disruption_try_at": None,
    "disruption_fails":  0,
    "wdt_skip":   False,       # hang watchdog: skip-one-cycle latch (see below)
    "low_power":  False,       # latched low-battery pause state
    "quiet_override_at": None, # "HH:MM" A was pressed during quiet hours (§9.17)
    "utc_migrated": False,     # one-time: RTC moved from the old LOCAL-time model
                               # to UTC (§9.16). Latched after the first NTP re-seed
                               # so migration never repeats. Absent in old State
                               # files, so a first run of this build defaults False.
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
weather_fails = persist.get("weather_fails") or 0
disruption        = persist.get("disruption")
disruption_type   = persist.get("disruption_type")
disruption_at     = persist.get("disruption_at")
disruption_try_at = persist.get("disruption_try_at")
disruption_fails  = persist.get("disruption_fails") or 0

# ---- Hang watchdog (§9.19) -------------------------------------------------
# powman.sleep() can hang with the chip powered (frozen panel, draining battery),
# beyond the reach of try/except. Armed per battery wake, fed before each slow
# step; deep sleep powers the chip off, so it never trips mid-sleep. Not armed on
# USB, where the framework's wait loop never sleeps and would trip it. The first
# wake after a watchdog reboot only sleeps, so it can't boot-loop.

WDT_MS = 16000   # RP2350 max is ~16.7 s; falls back to 8000 if refused

_wdt = None
_wdt_checked = False

def _wdt_start():
    """Arm the hardware watchdog once per boot, on battery only."""
    global _wdt
    if _wdt is not None:
        return
    try:
        if badge.usb_connected():
            return
    except Exception:
        return
    for ms in (WDT_MS, 8000):
        try:
            _wdt = machine.WDT(timeout=ms)
            return
        except Exception:
            pass

def _feed():
    """Tell the watchdog we're still alive. No-op when it isn't armed."""
    if _wdt is not None:
        try:
            _wdt.feed()
        except Exception:
            pass

def _woken_by_watchdog():
    try:
        import powman
        return badge.wake_reason() == powman.WAKE_WATCHDOG
    except Exception:
        return False

def _skip_after_watchdog():
    """True if this wake should do nothing but sleep (see boot-loop guard)."""
    global _wdt_checked
    if _wdt_checked:
        return False
    _wdt_checked = True
    if _woken_by_watchdog() and not persist.get("wdt_skip"):
        persist["wdt_skip"] = True
        save_state()
        return True
    if persist.get("wdt_skip"):
        persist["wdt_skip"] = False
        save_state()
    return False

# ---- Input helpers ---------------------------------------------------------

def _button_a_down():
    """True if button A registered a press this cycle (badge.pressed; §5.5)."""
    try:
        return badge.pressed(BUTTON_A)
    except Exception:
        return False

def _woken_by_button():
    """True if a button was the deep-sleep wake reason. Used by the quiet-hours
    override (§9.17): a quick tap that wakes the device may be released before
    the first badge.pressed() poll of the reloaded module, so the wake reason is
    the reliable signal. UNVERIFIED on-device in this role — if it ever reports
    True on ALARM wakes the override would never expire (validation item, §12);
    the fallback is to drop this from the trigger OR in update()."""
    try:
        return badge.woken_by_button()
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
# The RTC holds UTC and keeps it off the LiPo through deep sleep. UK wall-clock
# is derived at READ time by london_now(), which adds +1h during British Summer
# Time — worked out from the UTC instant via the last-Sunday rule below. Storing
# UTC (not local) is what makes BOTH the twice-yearly clocks-change AND a
# battery-death NTP recovery correct all year round: the March/October switch is
# just a different answer from the same rule (no re-sync needed), and NTP already
# returns UTC, so there's no offset to double-count on recovery (§9.16). Every
# wall-clock read goes through london_now(); rtc.datetime() is read directly only
# for the UTC-invariant year (clock_valid()).
#
# MIGRATION: older builds stored UK LOCAL time in the RTC. migrate_rtc_to_utc_if_
# needed() re-seeds it from NTP (UTC) once on first run of this build and latches
# persist["utc_migrated"] so it never runs again (§9.16).

def _last_sunday(year, month):
    """Day-of-month of the last Sunday in `month`. Only ever called for March and
    October (both 31 days), so 31 is a safe starting point."""
    day = 31
    wd = time.localtime(time.mktime((year, month, day, 12, 0, 0, 0, 0)))[6]  # 0=Mon..6=Sun
    return day - ((wd - 6) % 7)

def _is_bst(y, mo, d, h):
    """True if the UTC instant (y, mo, d, h) falls in British Summer Time. BST
    runs 01:00 UTC on the last Sunday of March -> 01:00 UTC on the last Sunday of
    October. Deciding from the UTC instant sidesteps the ambiguous/skipped local
    hour at each boundary entirely (§9.16)."""
    if mo < 3 or mo > 10:
        return False
    if 3 < mo < 10:
        return True
    ls = _last_sunday(y, mo)
    if mo == 3:                      # spring forward at 01:00 UTC
        if d != ls:
            return d > ls
        return h >= 1
    if d != ls:                      # October: fall back at 01:00 UTC
        return d < ls
    return h < 1

def london_now():
    """UK wall-clock (BST-aware) as a time.localtime()-style tuple
    (year, month, day, hour, minute, second, weekday, yearday). The RTC holds
    UTC; this is the single seam that converts it to local time, adding +1h in
    summer via _is_bst(). All wall-clock reads in the app go through here (§9.16)."""
    try:
        y, mo, d, h, mi, s, _ = rtc.datetime()
        secs = time.mktime((y, mo, d, h, mi, s, 0, 0))
        if _is_bst(y, mo, d, h):
            secs += 3600
        return time.localtime(secs)
    except Exception:
        return time.localtime()

def clock_valid():
    """False when the RTC has reset to its 2000 epoch (post battery death; §9.13).
    Reads rtc.datetime() directly — the year is the same in UTC or local, so no
    conversion is needed here. Gates the NTP re-sync, quiet hours (§9.17 — never
    trust an unset clock to decide it's night-time) and the disruption date
    window (§9.18)."""
    try:
        return rtc.datetime()[0] >= RTC_MIN_VALID_YEAR
    except Exception:
        return False

def sync_clock_if_needed():
    """No-op unless the RTC looks unset (clock_valid() False), i.e. after a full
    battery death. Then re-seed it from NTP (UTC) and mirror into the system
    clock. Self-healing: a failed NTP attempt leaves the clock unset and retries
    next refresh. Because the RTC is stored as UTC and london_now() applies the
    BST offset at read time, an NTP recovery is now correct year-round — the old
    "1h behind during BST" caveat is gone (§9.16)."""
    try:
        if clock_valid():
            return
        rtc.time_from_ntp()      # seeds the RTC in UTC — matches our stored model
        rtc.rtc_to_localtime()   # mirror into time.localtime()
    except Exception:
        pass

def migrate_rtc_to_utc_if_needed():
    """One-time migration. Older builds stored UK LOCAL time in the RTC; this
    build stores UTC and converts at read (london_now). Re-seed once from NTP —
    which returns UTC, our new model — then latch a flag so it never runs again.
    Requires WiFi to be up; the caller (do_refresh, post-connect) guarantees it.
    A failure just leaves the flag clear to retry on the next refresh (§9.16)."""
    if persist.get("utc_migrated"):
        return
    try:
        rtc.time_from_ntp()
        rtc.rtc_to_localtime()
        persist["utc_migrated"] = True
        save_state()
    except Exception:
        pass

# ---- Quiet hours (§8 / §9.17) -----------------------------------------------

def _hhmm_to_mins(s):
    """'HH:MM' -> minutes since midnight, or None if unparseable."""
    try:
        hh, mm = s.split(":")
        return (int(hh) % 24) * 60 + (int(mm) % 60)
    except Exception:
        return None

def in_quiet_hours():
    """True inside the configured quiet window (midnight wraparound supported).
    Always False if disabled (either bound None), misconfigured, or the clock
    can't be trusted — an epoch-reset clock reading 00:00 must not park the
    device in quiet mode and stop it ever fixing its own clock (§9.17)."""
    if QUIET_START is None or QUIET_END is None:
        return False
    if not clock_valid():
        return False
    start = _hhmm_to_mins(QUIET_START)
    end   = _hhmm_to_mins(QUIET_END)
    if start is None or end is None or start == end:
        return False
    t = london_now()
    now = t[3] * 60 + t[4]
    if start < end:
        return start <= now < end
    return now >= start or now < end          # window wraps midnight

def mins_until_quiet_end():
    """Minutes from now to QUIET_END (>= 1 while inside the window)."""
    end = _hhmm_to_mins(QUIET_END)
    if end is None:
        return QUIET_SLEEP_MINS
    t = london_now()
    now = t[3] * 60 + t[4]
    d = end - now
    if d <= 0:
        d += 24 * 60
    return d

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
                _feed()
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
    _feed()
    import requests
    try:
        return requests.get(url, timeout=REQ_TIMEOUT)
    except TypeError:
        return requests.get(url)

# ---- Stop disruption (§9.18) ------------------------------------------------

def _collapse_ws(s):
    """Collapse TfL's embedded newlines and cosmetic indentation into one line.
    The live payload is literally:
        'Bus Stop Closed\\n    Please use the next stop\\n      or the previous
         stop \\n       to catch your bus'
    — formatted for a web page, not a 264px panel. Written as a char loop rather
    than ' '.join(s.split()) purely to stay obvious about what it does; both are
    fine on MicroPython."""
    out = []
    prev_space = True          # True so leading whitespace is dropped
    for ch in s:
        if ch in " \t\r\n":
            if not prev_space:
                out.append(" ")
            prev_space = True
        else:
            out.append(ch)
            prev_space = False
    return "".join(out).strip()

def _window_active(item):
    """Is this disruption live TODAY? fromDate/toDate are ISO-8601, and ISO dates
    sort correctly as plain strings, so a lexical compare of the 'YYYY-MM-DD'
    prefix does the whole job — no date parsing on MicroPython.

    Fails OPEN in every ambiguous case (unset clock, missing or malformed bounds):
    a closure we cannot date-check is better shown than hidden (§9.18)."""
    if not clock_valid():
        return True
    try:
        t = london_now()
        today = "{:04d}-{:02d}-{:02d}".format(t[0], t[1], t[2])
        frm = item.get("fromDate") or ""
        if len(frm) >= 10 and today < frm[:10]:
            return False
        to = item.get("toDate") or ""
        if len(to) >= 10 and today > to[:10]:
            return False
    except Exception:
        pass
    return True

def fetch_disruption():
    """Refresh the stop-closure state. Success ALWAYS writes the result, including
    the empty case — a successful [] is how a closure ends, and is the only thing
    that clears the warning. Failure leaves the previous state untouched and just
    bumps the counter (inverted fail-safe vs weather; see DISRUPTION_RETRY_BURST)."""
    global disruption, disruption_type, disruption_at, disruption_try_at, disruption_fails
    t = london_now()
    disruption_try_at = "{:02d}:{:02d}".format(t[3], t[4])
    ok = False
    try:
        r = _get(DISRUPTION_URL)
        if r.status_code == 200:
            data = r.json()
            r.close()
            note = None
            kind = None
            for item in data:
                if not _window_active(item):
                    continue
                kind = item.get("type") or "Disruption"
                note = _collapse_ws(item.get("description") or kind)
                if kind == "Closure":
                    break     # a closure outranks any lesser Information notice,
                              # so stop looking once one is found
            disruption      = note
            disruption_type = kind
            disruption_at   = disruption_try_at
            ok = True
        else:
            r.close()
    except Exception:
        pass
    disruption_fails = 0 if ok else min(disruption_fails + 1, 99)
    save_state()

def disruption_due():
    """Hourly, but every wake while inside a retry burst — same shape as
    weather_due() (§8)."""
    if disruption_try_at is None:
        return True
    if 0 < disruption_fails <= DISRUPTION_RETRY_BURST:
        return True
    m = minutes_since(disruption_try_at)
    if m is None:
        return True
    return m >= DISRUPTION_MAX_STALE_MINS

def stop_closed():
    """True when the live disruption is a stop CLOSURE — the only type that
    justifies moving the arrivals fetch to another stop."""
    return disruption is not None and disruption_type == "Closure"

def active_stop():
    """(stop_id, stop_name) to fetch and title with THIS cycle. The single source
    of truth for failover: arrivals, the header and the alert all read it, so they
    can't disagree about which stop is on screen (§9.18)."""
    if stop_closed() and ALT_STOP_ID:
        return ALT_STOP_ID, ALT_STOP_NAME
    return STOP_ID, STOP_NAME

ALERT_SUFFIX = " Closed"

def alert_text():
    """One line for the alert bar, or None when there's nothing to say.

    Names the HOME stop, not the one being displayed: the header already says
    which stop the arrivals below belong to, so the bar's job is to explain WHY
    it changed. Read together — header "TFL  Buckingham Palace Rd", bar "Abbey
    Road Closed" — that's the whole story in two lines.

    We deliberately DON'T show TfL's own description on a closure: it runs to ~76
    characters ("...use the next stop or the previous stop to catch your bus"),
    which at 6px/char is 456px against a 264px panel, and the advice is already spent
    — the panel is showing that stop. Non-closure notices are rarer and
    unmodelled, so those pass TfL's text straight through."""
    if disruption is None:
        return None
    if stop_closed():
        # Trim the NAME against the remaining width, never the suffix: letting
        # _fit() loose on the whole string could yield e.g. "Long Stop Name Clos~"
        # and lose the only word that matters.
        try:
            w, _ = screen.measure_text(ALERT_SUFFIX)
        except Exception:
            w = 0
        return _fit(STOP_NAME, screen.width - 8 - w) + ALERT_SUFFIX
    return disruption

# ---- Arrivals / weather -----------------------------------------------------

def fetch_arrivals():
    """True on success; on failure leaves old `buses` intact. Queries whichever
    stop active_stop() nominates, so a closure silently redirects it (§9.18)."""
    global buses, last_ok_at
    try:
        stop_id, _ = active_stop()
        r = _get(arrivals_url(stop_id))
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
            if len(destination) > 23:
                destination = destination[:22] + "~"
            result.append({"line": line, "destination": destination, "minutes": eta_min})
        result.sort(key=lambda b: b["minutes"])
        buses = result[:MAX_BUSES]
        t = london_now()
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
        persist["weather_fails"]  = weather_fails
        persist["disruption"]        = disruption
        persist["disruption_type"]   = disruption_type
        persist["disruption_at"]     = disruption_at
        persist["disruption_try_at"] = disruption_try_at
        persist["disruption_fails"]  = disruption_fails
        State.save("tfl", persist)
    except Exception:
        pass

def fetch_weather():
    """Silent failure (keeps old data). Stamps weather_try_at on every attempt
    for the hourly backoff, and counts consecutive failures in weather_fails to
    drive the retry burst (§8); saves on all paths."""
    global weather, weather_at, weather_try_at, weather_fails
    t = london_now()
    weather_try_at = "{:02d}:{:02d}".format(t[3], t[4])
    ok = False
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
            ok = True
        else:
            r.close()
    except Exception:
        pass
    weather_fails = 0 if ok else min(weather_fails + 1, 99)
    save_state()

# ---- Refresh orchestration -------------------------------------------------

def do_refresh(forced=False):
    """Reconnect, refresh disruption when due, fetch buses, fetch weather when
    due; degrade to stale/error when offline (see §8). `forced` is set by a manual
    A press and skips the hourly gate on both the disruption and weather fetches."""
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
    migrate_rtc_to_utc_if_needed()   # one-time: move an old LOCAL-time RTC to UTC (§9.16)

    # ORDER MATTERS: disruption first. It decides which stop fetch_arrivals()
    # queries, so running it second would leave the panel one cycle behind the
    # closure — showing the closed stop's times under a "closed" banner (§9.18).
    if forced or disruption_due():
        fetch_disruption()

    bus_ok = fetch_arrivals()
    if forced or weather_due():
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
        t = london_now()
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
    """True if a weather fetch is due: hourly normally, but every wake while a
    failed hourly attempt is still inside its retry burst (weather_fails in
    1..WEATHER_RETRY_BURST; §8). Any success resets the counter."""
    if weather_try_at is None:
        return True
    if 0 < weather_fails <= WEATHER_RETRY_BURST:
        return True
    m = minutes_since(weather_try_at)
    if m is None:
        return True
    return m >= WEATHER_MAX_STALE_MINS

def weather_fresh():
    """True if the footer should show: data younger than the stale threshold, or
    held over while the retry burst is still in flight (§8) — the footer only
    drops once the burst is exhausted without a success."""
    if weather is None or weather_at is None:
        return False
    if 0 < weather_fails <= WEATHER_RETRY_BURST:
        return True
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

def _fit(s, max_w):
    """Trim `s` to fit max_w pixels, marking the cut with '~' — same convention as
    the destination truncation in fetch_arrivals(). Measures rather than counting
    characters because rom_font.smart is proportional."""
    try:
        w, _ = screen.measure_text(s)
        if w <= max_w:
            return s
        while len(s) > 1:
            s = s[:-1]
            w, _ = screen.measure_text(s + "~")
            if w <= max_w:
                return s + "~"
    except Exception:
        pass
    return s

def _draw_header(bat_str, lt):
    """Black header bar: battery meter LHS, title, clock RHS. Shared by the
    normal and quiet-hours screens. Titles with active_stop() so the name always
    matches the arrivals below it under failover (§9.18). Leaves the pen black."""
    screen.pen = color.black
    screen.rectangle(0, 0, screen.width, 18)
    screen.pen = color.white
    screen.font = rom_font.smart
    # y=2, not the arithmetically-centred 3: rom_font.smart renders with a pixel
    # or two of padding above the glyph, so 3 sits visibly low in an 18px bar.
    # All three below must move together. _draw_alert() uses top+2 to match (§7).
    if bat_str:
        screen.text(bat_str, 4, 2)
    screen.text("TFL  {}".format(active_stop()[1]), 50, 2)
    ts = "{:02d}:{:02d}".format(lt[3], lt[4])
    w, _ = screen.measure_text(ts)
    screen.text(ts, screen.width - w - 4, 2)
    screen.pen = color.black

ALERT_H = 18    # Alert bar height. Set from the panel, not from arithmetic (§5
                # house rule): at 15 the text sat low in the bar with a couple of
                # dead rows above it; at 18 it centres with one row of black top
                # and bottom. Happens to match the header bar, which is why the
                # two read as a pair. All six bus rows still fit alongside it.

FOOTER_RULE_Y = 18   # distance from the panel bottom to the weather rule, i.e.
                     # the `footer_y - 4` in _draw_weather_footer(). Named because
                     # it's now shared with _alert_top() and equal to ALERT_H by
                     # coincidence — don't let a future tweak conflate them.

def _alert_top():
    """y of the alert bar — the second-to-last line. Sits directly on top of the
    weather rule when the footer is showing, and drops to the bottom line of the
    panel when it isn't, so it's always the last thing above the weather and never
    leaves a gap (§9.18)."""
    if weather_fresh():
        return screen.height - FOOTER_RULE_Y - ALERT_H
    return screen.height - ALERT_H

def _rows_bottom(alert):
    """Lowest y a bus row may start at, or None for no limit.

    A safety net, not a design constraint: on-device all six rows fit above the
    bar with room to spare, so this never fires in normal use. It's here so that a
    future MAX_BUSES bump, a taller ALERT_H or a wider font degrades by dropping
    the last row rather than by drawing bus times underneath a black bar. Only
    clamps while the bar is up; the normal screen is the original layout
    untouched (§9.18)."""
    if not alert:
        return None
    return _alert_top() - 2

def _draw_alert(msg):
    """Inverted (white-on-black) notice on the second-to-last line, text centred.

    Below the bus rows rather than in the y=22 status slot, so the
    Line/Destination/Min headings and the 'Stale' message keep their row and the
    table still reads as a table. Inverted rather than plain text so it reads as
    an exception, not as data. Drawn LAST so it lands over a clean background
    whatever ran before it. Leaves the pen black."""
    top = _alert_top()
    screen.pen = color.black
    screen.rectangle(0, top, screen.width, ALERT_H)
    screen.pen = color.white
    screen.font = rom_font.smart
    msg = _fit(msg, screen.width - 8)
    w, _ = screen.measure_text(msg)
    screen.text(msg, (screen.width - w) // 2, top + 2)
    screen.pen = color.black

def _draw_weather_footer(lt):
    """Weather footer; no-op unless weather_fresh(). Shared by the normal and
    quiet-hours screens."""
    if not weather_fresh():
        return
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

def draw_quiet_screen(bat_str, lt):
    """Quiet-hours notice (§9.17): live header (battery/clock) and weather footer
    around a static message. Repainted once per QUIET_SLEEP_MINS heartbeat, not
    per wake/loop. Deliberately does NOT surface the disruption alert — the
    heartbeat doesn't fetch it, so anything drawn here could be up to a night old,
    and normal refreshes resume at QUIET_END anyway (§9.18)."""
    screen.pen = color.white
    screen.clear()
    _draw_header(bat_str, lt)
    _ctext("Buses are still running -", 30)
    _ctext("I'm asleep to conserve battery.", 44)
    _ctext("Auto-updates resume at {}.".format(QUIET_END), 64)
    _ctext("Press A for an update now.", 84)
    _draw_weather_footer(lt)

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
        _feed()
        wait_for_button_or_alarm(timeout=AWAKE_MS)

def update():
    global initialised, last_refresh_minute, last_quiet_draw_minute

    _wdt_start()                        # hang watchdog (battery only; see above)
    _feed()
    if _skip_after_watchdog():          # first wake after a watchdog reboot:
        arm_and_sleep(REFRESH_MINS)     # no network, no redraw, just sleep
        return

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
        _feed()
        badge.update()
        persist["low_power"] = True
        save_state()
        arm_and_sleep(LOW_POWER_SLEEP_MINS)
        return

    # ---- Quiet-hours gate (§8 / §9.17): park overnight, hourly heartbeat -------
    # Sits below the low-battery gate (battery safety wins) and above the normal
    # refresh path. in_quiet_hours() is False when disabled or the clock is
    # untrusted, so the post-death NTP self-heal still runs via the normal path.
    if in_quiet_hours():
        # Button A — pressed this cycle, or the reason we woke from deep sleep
        # (a quick tap can be released before the reloaded module's first poll)
        # — (re)starts a live-updates override. woken_by_button() is unverified
        # in this role: if it reports True on ALARM wakes the override never
        # expires — see §12 validation; fallback is to drop it from this OR.
        if _button_a_down() or _woken_by_button():
            t = london_now()
            persist["quiet_override_at"] = "{:02d}:{:02d}".format(t[3], t[4])
            save_state()
        ov = minutes_since(persist.get("quiet_override_at"))
        if ov is not None and ov < QUIET_OVERRIDE_MINS:
            pass   # override active -> fall through to normal live operation
        else:
            if persist.get("quiet_override_at") is not None:
                persist["quiet_override_at"] = None   # expired: clear it so the
                save_state()              # HH:MM stamp can't wrap around 24h and
                                          # spuriously re-trigger tomorrow night
                last_quiet_draw_minute = -1   # repaint over the override's bus UI
            t = london_now()
            now_min = t[3] * 60 + t[4]
            elapsed_q = now_min - last_quiet_draw_minute
            if elapsed_q < 0:                          # crossed midnight
                elapsed_q += 24 * 60
            # Hourly heartbeat: weather-only fetch when due, then repaint the
            # header clock/battery + footer. On battery every wake is a fresh
            # reload (last_quiet_draw_minute = -1) so each chunk repaints; on
            # USB (loop spins, no deep sleep) this gate stops constant redraws.
            if last_quiet_draw_minute < 0 or elapsed_q >= QUIET_SLEEP_MINS:
                if weather_due():
                    if not wifi_connected():
                        connect_wifi()
                    if wifi_connected():
                        fetch_weather()
                draw_quiet_screen(bat_str, london_now())
                _feed()
                badge.update()
                last_quiet_draw_minute = now_min
            # Chunked sleep, capped so the first normal refresh lands at QUIET_END
            arm_and_sleep(min(QUIET_SLEEP_MINS, mins_until_quiet_end()))
            return
    elif persist.get("quiet_override_at") is not None:
        persist["quiet_override_at"] = None   # left the quiet window; tidy the
        save_state()                          # latch (same 24h-wrap reasoning)
        last_quiet_draw_minute = -1

    t = london_now()
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

    if _button_a_down():                # manual: also forces weather + disruption
        should_refresh = True
        manual = True

    if should_refresh:
        do_refresh(forced=manual)
        last_refresh_minute = current_minute

    # ---- Draw ----
    screen.pen = color.white
    screen.clear()

    lt = london_now()                   # UK wall-clock; header clock + footer date

    _draw_header(bat_str, lt)

    # The alert gets its own slot near the bottom, so the y=22 status row keeps
    # its original precedence untouched: stale > column headings (§9.18).
    alert = alert_text()

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
        bottom = _rows_bottom(alert)          # None unless the alert bar is up
        _, text_h = screen.measure_text("0")  # measured, not assumed: rom_font.smart
                                              # is proportional and its height isn't
                                              # documented (§5.1)
        screen.pen = color.black
        for bus in buses:
            if bottom is not None and y + text_h > bottom:
                break                         # yield the row to the alert bar
            min_str = "Due" if bus["minutes"] == 0 else str(bus["minutes"])
            screen.text(bus["line"],        4,  y)
            screen.text(bus["destination"], 50, y)
            w, _ = screen.measure_text(min_str)
            screen.text(min_str, screen.width - w - 4, y)
            y += row_h

        # Weather footer (hidden once stale; held through a retry burst — §8)
        _draw_weather_footer(lt)

    # Alert bar last: it owns the second-to-last line outright, above whatever
    # ran before it and above the weather footer (§9.18).
    if alert:
        _draw_alert(alert)

    # Push e-ink only when we refreshed (avoids constant redraw on USB; §5.3)
    if should_refresh:
        _feed()
        badge.update()

    arm_and_sleep(REFRESH_MINS)   # verified-retry arm + deep sleep (§5.4 / §9.12)

def on_exit():
    try:
        rtc.clear_alarm()   # don't leave a stale wake armed (matches stock apps)
    except Exception:
        pass

run(update)
