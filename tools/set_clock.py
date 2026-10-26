# set_clock.py — one-off REPL tool: re-seed the Badger 2350 RTC from NTP.
#
# The RTC chip holds UTC, and the app's london_now() adds the BST hour for the
# display (HANDOFF §5.4, §9.16). So the chip must be seeded with plain NTP UTC:
# never write local time into it, or the header reads an hour fast in summer.
# Both lines printed below read UTC; that is expected.
#
# Why: the RTC drifts (~1 min 40 s over three months) and the app only re-syncs
# after a full battery death (clock_valid() False), never for drift.
#
# How to run (Thonny, badge on USB, app stopped so you have the >>> prompt):
#   1. Copy this file to /system/apps/tfl/set_clock.py (double-tap RESET disk mode)
#   2. In the REPL:  exec(open("/system/apps/tfl/set_clock.py").read())
#   3. Check "rtc after" reads UTC (your watch minus 1h in BST, equal in winter),
#      then RESET the badge and check the header against your watch.
#
# Uses the injected `rtc` global (present in the REPL environment; NOT `import rtc`).

import sys, time, network
sys.path.insert(0, "/system/apps/tfl")
import secrets

try:
    rtc
except NameError:
    raise SystemExit("No injected `rtc` global in this REPL — run from the normal badge REPL, not a bare interpreter.")

# --- WiFi (bounded, mirrors connect_wifi() in __init__.py) -------------------
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
if not wlan.isconnected():
    wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
    for _ in range(30):
        if wlan.isconnected():
            break
        time.sleep(0.5)
if not wlan.isconnected():
    raise SystemExit("WiFi did not come up — NTP needs a connection (else OSError -6).")

print("rtc before :", rtc.datetime())

# --- NTP -> RTC chip (UTC), with retry: ntptime makes one UDP attempt and
# raises OSError 110 (ETIMEDOUT) when it is dropped ------------------------------
ok = False
for i in range(6):
    try:
        rtc.time_from_ntp()
        ok = True
        break
    except OSError as e:
        print("ntp attempt", i + 1, "failed:", e)
        time.sleep(2)

rtc.rtc_to_localtime()     # chip -> system clock (UTC; london_now() localises)

print("ntp ok     :", ok)
print("rtc after  :", rtc.datetime(), " (UTC)")
print("sys        :", time.localtime(), " (UTC; the header adds the BST hour)")
