"""Constants for the Changan Deepal Cloud integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "deepal"
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.LOCK,
    Platform.COVER,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.TIME,
]

CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_VEHICLE_ID = "vehicle_id"
CONF_COUNTRY = "country"
CONF_LANGUAGE = "language"
CONF_APP_VERSION = "app_version"
CONF_DEVICE_ID = "device_id"
CONF_PRIVATE_KEY = "private_key_pem"
CONF_ENABLE_COMMANDS = "enable_commands"
CONF_ACTIVE_REFRESH_INTERVAL = "active_refresh_interval"
CONF_RC_TOKEN = "rc_token"
CONF_CONTROL_PIN = "control_pin"
CONF_CAC_TOKEN = "cac_token"
CONF_USER_ID = "user_id"
CONF_CA_USER_ID = "ca_user_id"
CONF_CAC_USER_ID = "cac_user_id"

DEFAULT_COUNTRY = "GB"
DEFAULT_LANGUAGE = "en_US"
DEFAULT_APP_VERSION = "V1.11.0"
DEFAULT_SCAN_INTERVAL = 60
DEFAULT_ACTIVE_REFRESH_INTERVAL = 300

BASE_URL = "https://m.iov.changanauto.com.de"

# 2048-bit RSA public key used by the app to encrypt mobile/password/control PIN fields.
# Extracted from the live app's Dalvik heap and validated against send-auth-code:
# RSA/ECB/PKCS1Padding succeeds; OAEP variants fail.
REQUEST_ENCRYPTION_PUBLIC_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAkyhr43cBPTJ3jLiYsmbUwUp74cMJIOju5vqVzgtuK63Q99qV6iVT8wN5cXlyMtWI2mfOmhIao/fUN821im69MfOHsWXdqQEo5e9v654GPw+bju0pCphEPtD1I0VcyS34QkAu04urSun2U1q3Dr2OICLVWSnLa+01ioKxkaB0D209zXcls2eFQpvRAWm7xxVsoqzSwqp+neu5quOpn+eO/bW0TxcSQ8VZcDEUvadRTLSR0eOWgRuHIBiD2RGqPIPzKCm5A14q1qhxUZ8U0pmYe0Sx7eMy4RVe2iW7fnjc6pxTUMBkercSL26mevYouuCKqyie+LVQAtGa29RMl/lyiwIDAQAB"
)
