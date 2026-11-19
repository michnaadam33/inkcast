# Copy this file to config.py, fill in your values and upload it to the Pico.
# config.py is git-ignored - never commit real credentials.

WIFI_SSID = "your-wifi-name"
WIFI_PASSWORD = "your-wifi-password"

# ThingSpeak channel -> API Keys -> Write API Key
THINGSPEAK_API_KEY = "XXXXXXXXXXXXXXXX"

# Tuya IoT Platform (https://iot.tuya.com) -> Cloud -> your project
TUYA_CLIENT_ID = "your-access-id"
TUYA_SECRET = "your-access-secret"
TUYA_DEVICE_ID = "your-device-id"
# Data center of your Tuya project:
#   Central Europe: https://openapi.tuyaeu.com
#   Western America: https://openapi.tuyaus.com
#   China: https://openapi.tuyacn.com
#   India: https://openapi.tuyain.com
TUYA_BASE = "https://openapi.tuyaeu.com"
