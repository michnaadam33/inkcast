"""
inkcast - battery-powered e-Paper weather station on Raspberry Pi Pico W.

The TPL5110 cuts power to the Pico between cycles (interval set by the
resistor on its DELAY pin), so every boot runs exactly one cycle:
  1) read the indoor BME280 sensor (temperature, humidity, pressure),
  2) connect to WiFi, read the outdoor Tuya sensor and upload to ThingSpeak,
  3) refresh the e-Paper display,
  4) pulse the DONE pin so the TPL5110 cuts power immediately.

Required files next to main.py on the Pico:
  - config.py   - your credentials (copy config.example.py)
  - bme280.py
  - epd2in13.py - Waveshare e-Paper driver
"""

import network
import time
import ntptime
import gc
import hashlib
import ubinascii
import urequests
from machine import Pin, I2C

from bme280 import BME280
from config import (WIFI_SSID, WIFI_PASSWORD, THINGSPEAK_API_KEY,
                    TUYA_CLIENT_ID, TUYA_SECRET, TUYA_DEVICE_ID, TUYA_BASE)

try:
    # The EPD object is itself the frame buffer (inherits framebuf.FrameBuffer).
    from epd2in13 import EPD_2in13_V4_Landscape as EPD
except ImportError as e:
    EPD = None
    print("e-Paper driver not loaded:", e)

# ===================== PINS =====================
DONE_PIN = 16     # GP16 -> TPL5110 DONE input
I2C_SDA_PIN = 0   # BME280
I2C_SCL_PIN = 1   # BME280
# e-Paper (Waveshare Pico-ePaper-2.13): DIN-GP11, CLK-GP10, CS-GP9,
#   DC-GP8, RST-GP12, BUSY-GP13, VCC-VSYS, GND-GND
# =========================================================

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
UNIX_EPOCH_OFFSET = 946684800  # MicroPython epoch starts in 2000


# ---------------------- Sensors / network ----------------------

def read_bme280():
    """Return (temperature_C, humidity_percent, pressure_hPa) or Nones."""
    try:
        i2c = I2C(0, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=100000)
        temperature, pressure, humidity = BME280(i2c).read()
        return temperature, humidity, pressure
    except Exception as e:
        print("BME280 read error:", e)
        return None, None, None


def connect_wifi(timeout_s=20):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    for _ in range(timeout_s):
        if wlan.isconnected():
            print("WiFi connected, IP:", wlan.ifconfig()[0])
            return wlan
        time.sleep(1)
    print("WiFi connection failed.")
    wlan.active(False)
    return None


def _hmac_sha256(key, msg):
    key = key.encode()
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key + b"\x00" * (64 - len(key))
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in key) + msg.encode()).digest()
    return hashlib.sha256(bytes(b ^ 0x5c for b in key) + inner).digest()


def _tuya_get(path, token=""):
    """Signed GET to Tuya Cloud; returns 'result' or raises with Tuya's error."""
    t = str(int((time.time() + UNIX_EPOCH_OFFSET) * 1000))
    string_to_sign = "GET\n" + EMPTY_SHA256 + "\n\n" + path
    sign = ubinascii.hexlify(
        _hmac_sha256(TUYA_SECRET, TUYA_CLIENT_ID + token + t + string_to_sign)
    ).decode().upper()
    headers = {"client_id": TUYA_CLIENT_ID, "sign": sign, "t": t,
               "sign_method": "HMAC-SHA256"}
    if token:
        headers["access_token"] = token
    gc.collect()
    r = urequests.get(TUYA_BASE + path, headers=headers)
    try:
        resp = r.json()
    finally:
        r.close()
    if not resp.get("success"):
        raise RuntimeError("Tuya error {}: {}".format(resp.get("code"), resp.get("msg")))
    return resp["result"]


def read_tuya(attempts=3):
    """Return (temperature_C, humidity_percent, battery_percent) or Nones."""
    for attempt in range(1, attempts + 1):
        try:
            token = _tuya_get("/v1.0/token?grant_type=1")["access_token"]
            status = _tuya_get("/v1.0/devices/{}/status".format(TUYA_DEVICE_ID), token)
            values = {x["code"]: x["value"] for x in status}
            return (values["va_temperature"] / 10, values["va_humidity"],
                    values.get("battery_percentage"))
        except Exception as e:
            print("Tuya attempt {} failed: {}".format(attempt, e))
            time.sleep(1)
    return None, None, None


def send_to_thingspeak(*fields):
    """Upload values as field1..fieldN; None values are sent empty."""
    params = "&".join(
        "field{}={}".format(i, "" if v is None else "{:.2f}".format(v))
        for i, v in enumerate(fields, 1))
    url = "http://api.thingspeak.com/update?api_key={}&{}".format(THINGSPEAK_API_KEY, params)
    try:
        r = urequests.get(url)
        print("ThingSpeak response:", r.status_code, r.text)
        r.close()
    except Exception as e:
        print("ThingSpeak upload error:", e)


# ---------------------- e-Paper display ----------------------
# framebuf only has a small 8x8 font, so large numbers are drawn
# as seven-segment digits built from rectangles.

SEGMENTS = {
    "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
    "5": "afgcd", "6": "afgecd", "7": "abc", "8": "abcdefg", "9": "abcdfg",
    "C": "afed",
}


def draw_digit(fb, x, y, char, w, h, t, color):
    half = h // 2 - t // 2
    if char == "-":
        fb.fill_rect(x, y + half, w, t, color)
        return
    if char == ".":
        fb.fill_rect(x, y + h - t, t, t, color)
        return
    seg = SEGMENTS.get(char, "")
    vert = h // 2 + t // 2
    if "a" in seg:
        fb.fill_rect(x, y, w, t, color)
    if "d" in seg:
        fb.fill_rect(x, y + h - t, w, t, color)
    if "g" in seg:
        fb.fill_rect(x, y + half, w, t, color)
    if "f" in seg:
        fb.fill_rect(x, y, t, vert, color)
    if "b" in seg:
        fb.fill_rect(x + w - t, y, t, vert, color)
    if "e" in seg:
        fb.fill_rect(x, y + half, t, vert, color)
    if "c" in seg:
        fb.fill_rect(x + w - t, y + half, t, vert, color)


def draw_big_number(fb, x_center, y, text, w, h, t, gap, color):
    """Draw text (digits, '.', '-') centered on x_center; return the end x."""
    widths = [w // 2 if c == "." else w for c in text]
    x = x_center - (sum(widths) + gap * (len(text) - 1)) // 2
    for char, cw in zip(text, widths):
        draw_digit(fb, x, y, char, cw, h, t, color)
        x += cw + gap
    return x - gap


def update_display(t_in, hum_in, pressure, t_out, hum_out):
    if not EPD:
        print("Skipping display - e-Paper driver not loaded.")
        return

    TOP = 16  # top margin (px)
    BLACK = 0x00

    def fmt_temp(v):
        return "{:.1f}".format(v) if v is not None else "--.-"

    def fmt_int(v, placeholder):
        return "{:.0f}".format(v) if v is not None else placeholder

    try:
        epd = EPD()
        epd.fill(0xff)

        def label(text, x_center, y):
            epd.text(text, x_center - len(text) * 4, y, BLACK)

        def column(x_temp, x_center, title, temp, hum):
            label(title, x_center, TOP)
            y = TOP + 12
            x_end = draw_big_number(epd, x_temp, y, fmt_temp(temp), 13, 28, 4, 2, BLACK)
            # degree sign + 'C' (framebuf has no degree glyph)
            epd.ellipse(x_end + 8, y + 3, 3, 3, BLACK)
            draw_digit(epd, x_end + 14, y, "C", 11, 20, 3, BLACK)
            label("HUMIDITY", x_center, TOP + 44)
            x_end = draw_big_number(epd, x_center - 6, TOP + 54, fmt_int(hum, "--"), 8, 12, 2, 3, BLACK)
            epd.text("%", x_end + 4, TOP + 56, BLACK)

        column(50, 62, "INSIDE", t_in, hum_in)
        column(175, 187, "OUTSIDE", t_out, hum_out)

        epd.vline(125, TOP, 66, BLACK)
        epd.hline(10, TOP + 68, 230, BLACK)

        label("PRESSURE", 125, TOP + 72)
        x_end = draw_big_number(epd, 125 - 14, TOP + 82, fmt_int(pressure, "----"), 8, 12, 2, 3, BLACK)
        epd.text("hPa", x_end + 4, TOP + 84, BLACK)

        epd.display(epd.buffer)
        epd.sleep()
        print("e-Paper display updated.")
    except Exception as e:
        print("e-Paper display error:", e)


# ---------------------- Main cycle ----------------------

def signal_done(done):
    """Pulse DONE until the TPL5110 cuts power (loops forever on USB power)."""
    while True:
        done.value(1)
        time.sleep_ms(5)
        done.value(0)
        time.sleep_ms(5)


def main():
    done = Pin(DONE_PIN, Pin.OUT, value=0)

    t_in, hum_in, pressure = read_bme280()
    t_out = hum_out = battery = None

    wlan = connect_wifi()
    if wlan:
        try:
            ntptime.settime()  # Tuya request signatures need the correct time
            t_out, hum_out, battery = read_tuya()
        except Exception as e:
            print("NTP error:", e)

        print("In: {} / {}  Pressure: {}  Out: {} / {}  Battery: {}".format(
            t_in, hum_in, pressure, t_out, hum_out, battery))
        send_to_thingspeak(t_in, hum_in, pressure, t_out, hum_out, battery)
        wlan.disconnect()
        wlan.active(False)

    update_display(t_in, hum_in, pressure, t_out, hum_out)

    print("Cycle finished - signalling DONE to TPL5110.")
    signal_done(done)


main()
