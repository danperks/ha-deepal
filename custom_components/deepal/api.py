"""Async client for the Changan Deepal cloud API."""

from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import UTC, datetime
import gzip
import hashlib
import json
import logging
import ssl
import struct
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession
from cryptography.hazmat.primitives import hashes, padding as symmetric_padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import (
    BASE_URL,
    CA_BASE_URL,
    DEFAULT_GATEWAY,
    REGION_GATEWAYS,
    REQUEST_ENCRYPTION_PUBLIC_KEY,
)

_LOGGER = logging.getLogger(__name__)

# The S05 Door_Lock motor enum is lock=1, unlock=2, unlike the tailgate's
# open=1/close=2 request enum.
_S05_LOCK = 1
_S05_UNLOCK = 2
_S05_OPEN = 1
_S05_CLOSE = 2
_S05_POSITION_OPEN = 100
_S05_POSITION_CLOSED = 0
_S05_WAKE_SETTLE_SECONDS = 2
_S05_COMMAND_TIMEOUT = 45
_S05_SUCCESS_CODES = (None, "", 0, "0", "000000", 200, "200", "OK", "SUCCESS", "success")
_S05_VEHICLE_ERROR_MESSAGES = {
    "VVCC_-1_-1_02_023": "door status does not meet the command requirements",
}

_TRANSIENT_RETRY_ATTEMPTS = 3
_TRANSIENT_RETRY_DELAY = 2

_REDACTED = "[redacted]"
_MAX_LOG_STRING_LENGTH = 500
_SAFE_LOG_HEADER_KEYS = ("selectcountry", "appversion", "language")
_SENSITIVE_EXACT_KEYS = {
    "access_token",
    "authcode",
    "authorization",
    "bearer",
    "cactoken",
    "cacuserid",
    "cacuser_id",
    "cac_token",
    "causerid",
    "ca_user_id",
    "control_pin",
    "cookie",
    "deviceid",
    "email",
    "hwi",
    "hwid",
    "hw_id",
    "mobile",
    "password",
    "phone",
    "private_key_pem",
    "privatekey",
    "public_key",
    "pubkey",
    "publickey",
    "rctoken",
    "refresh_token",
    "refreshtoken",
    "safecode",
    "seriralno",
    "serial",
    "sign",
    "signature",
    "sms",
    "token",
    "userid",
    "user_id",
    "vehicleid",
    "vin",
}
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "bearer",
    "cookie",
    "email",
    "key",
    "mobile",
    "password",
    "pem",
    "phone",
    "pin",
    "rctoken",
    "serial",
    "signature",
    "sms",
    "token",
    "vehicleid",
    "vin",
)


def _is_sensitive_log_key(key: Any) -> bool:
    """Return whether a JSON/header key should never be logged raw."""
    normalized = str(key).lower().replace("-", "_")
    compact = normalized.replace("_", "")
    return (
        normalized in _SENSITIVE_EXACT_KEYS
        or compact in _SENSITIVE_EXACT_KEYS
        or any(part in compact for part in _SENSITIVE_KEY_PARTS)
    )


def _redact_for_log(value: Any) -> Any:
    """Return a redacted, log-safe copy of an API payload."""
    if isinstance(value, dict):
        return {
            key: _REDACTED if _is_sensitive_log_key(key) else _redact_for_log(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_for_log(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_for_log(item) for item in value)
    if isinstance(value, str):
        if len(value) > _MAX_LOG_STRING_LENGTH:
            return f"{value[:_MAX_LOG_STRING_LENGTH]}...[truncated {len(value) - _MAX_LOG_STRING_LENGTH} chars]"
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def _safe_log_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return only non-sensitive headers useful for debugging country/region behavior."""
    return {key: headers[key] for key in _SAFE_LOG_HEADER_KEYS if key in headers}


def _mqtt_string(value: str) -> bytes:
    data = value.encode()
    return struct.pack("!H", len(data)) + data


def _mqtt_remaining_length(length: int) -> bytes:
    out = bytearray()
    while True:
        encoded = length % 128
        length //= 128
        if length:
            encoded |= 128
        out.append(encoded)
        if not length:
            return bytes(out)


def _mqtt_connect_packet(client_id: str, username: str, password: str) -> bytes:
    payload = _mqtt_string(client_id) + _mqtt_string(username) + _mqtt_string(password)
    variable = _mqtt_string("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60)
    body = variable + payload
    return bytes([0x10]) + _mqtt_remaining_length(len(body)) + body


def _mqtt_subscribe_packet(packet_id: int, topics: list[str]) -> bytes:
    payload = b"".join(_mqtt_string(topic) + b"\x01" for topic in topics)
    body = struct.pack("!H", packet_id) + payload
    return bytes([0x82]) + _mqtt_remaining_length(len(body)) + body


def _mqtt_publish_packet(topic: str, payload: dict[str, Any]) -> bytes:
    body = _mqtt_string(topic) + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return bytes([0x30]) + _mqtt_remaining_length(len(body)) + body


async def _mqtt_read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first = (await reader.readexactly(1))[0]
    multiplier = 1
    remaining = 0
    while True:
        byte = (await reader.readexactly(1))[0]
        remaining += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            break
        multiplier *= 128
    return first, await reader.readexactly(remaining)


def _mqtt_parse_publish(first: int, body: bytes) -> tuple[str, dict[str, Any], int | None]:
    pos = 0
    topic_len = struct.unpack("!H", body[pos : pos + 2])[0]
    pos += 2
    topic = body[pos : pos + topic_len].decode(errors="replace")
    pos += topic_len
    packet_id: int | None = None
    qos = (first >> 1) & 0x03
    if qos:
        packet_id = struct.unpack("!H", body[pos : pos + 2])[0]
        pos += 2
    payload = json.loads(body[pos:].decode())
    return topic, payload, packet_id


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def _s05_aes_decrypt(encrypted: str, secret_key: str, req_id: str) -> list[dict[str, Any]]:
    decryptor = Cipher(
        algorithms.AES(secret_key.encode()),
        modes.CBC(hashlib.md5(req_id.encode()).digest()),
    ).decryptor()
    padded = decryptor.update(_b64decode(encrypted)) + decryptor.finalize()
    unpadder = symmetric_padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    compressed = _b64decode(plaintext.decode().strip())
    decoded = json.loads(gzip.decompress(compressed).decode())
    return decoded if isinstance(decoded, list) else []


def _s05_aes_encrypt(data: list[dict[str, Any]], secret_key: str, req_id: str) -> str:
    compressed_b64 = base64.b64encode(
        gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
    )
    padder = symmetric_padding.PKCS7(128).padder()
    padded = padder.update(compressed_b64) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(secret_key.encode()),
        modes.CBC(hashlib.md5(req_id.encode()).digest()),
    ).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


def _s05_payload_req_id(payload: dict[str, Any]) -> str | None:
    """Return the request id from either S05 response envelope."""
    req_id = payload.get("r")
    if isinstance(req_id, str):
        return req_id
    for key in ("h", "header"):
        header = payload.get(key)
        if isinstance(header, dict) and isinstance(header.get("r"), str):
            return header["r"]
    return None


def _s05_command_response_items(payload: dict[str, Any], secret_key: str, req_id: str) -> list[dict[str, Any]]:
    """Decrypt the result items in an S05 command response."""
    items: list[dict[str, Any]] = []
    for field in ("rs", "sers"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            try:
                items.extend(_s05_aes_decrypt(value, secret_key, req_id))
            except (ValueError, binascii.Error, json.JSONDecodeError, gzip.BadGzipFile) as err:
                raise DeepalApiError(f"Could not decrypt S05 command response: {err}") from err
        elif isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    return items


def _s05_command_result_error(items: list[dict[str, Any]], payload: dict[str, Any]) -> str | None:
    """Return an explicit vehicle error from a correlated S05 command response."""
    candidates: list[dict[str, Any]] = [payload, *items]
    index = 0
    while index < len(candidates):
        for key in ("h", "header", "d", "data", "p", "params"):
            nested = candidates[index].get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
        index += 1

    for item in candidates:
        if item.get("success") is False:
            return str(item.get("msg") or item.get("message") or "vehicle rejected the command")
        if item.get("success") is True:
            continue
        for key in ("resultCode", "result_code", "code"):
            code = item.get(key)
            if code in _S05_SUCCESS_CODES:
                continue
            message = str(item.get("msg") or item.get("message") or "")
            for error_code, detail in _S05_VEHICLE_ERROR_MESSAGES.items():
                if error_code == str(code) or error_code in message:
                    return f"{detail} ({error_code})"
            return message or f"result code {code}"
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_to_millis(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _first(params: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = params.get(key)
        if value is not None:
            return value
    return None


def _s05_condition_from_params(params: dict[str, Any]) -> dict[str, Any]:
    latest = _first(params, "latestDate", "lastUpdatedAt")
    condition: dict[str, Any] = {
        "lastUpdatedAt": _iso_to_millis(latest) or int(time.time() * 1000),
        "vehicleStatus": {
            "soc": _as_int(_first(params, "soc", "socDsp", "remainPower")),
            "drvMileage": _as_int(_first(params, "remainedPowerMile", "totalResidualMileage")),
            "totalMileage": _as_float(params.get("totalOdometer")),
            "engineSts": _as_int(params.get("engineStatus")),
            "connectStatus": 1,
            "powerStatus": _as_int(params.get("powerStatusFeedBack")),
            "epbSts": _as_int(params.get("electronichandbrakeStatus")),
            "steeringWheelHeater": _as_int(params.get("steeringWheelHeating")),
            "steeringWheelHeaterLevel": _as_int(params.get("steeringWheelHeating")),
        },
        "hvac": {
            "insideTemp": _as_float(params.get("vehicleTemperature")) * 10
            if _as_float(params.get("vehicleTemperature")) is not None
            else None,
            "insideHumidity": _as_float(params.get("innerHumidity")),
            "remoteTemp": _as_float(params.get("airConditioningSetTemperature")) * 10
            if _as_float(params.get("airConditioningSetTemperature")) is not None
            else None,
            "acStatus": _as_int(params.get("airStatus")),
            "defrostStatus": _as_int(params.get("frontDefrostStatus")),
            "insideAirQualityLevel": _as_int(params.get("airPurifierStatus")),
            "airRecycleStatus": _as_int(params.get("airRecycleStatus")),
            "fanLevel": _as_int(params.get("airConditioningHairRatings")),
        },
        "charge": {
            "chargeStatus": _as_int(params.get("ChrgSts")),
            "chargeConStatus": _as_int(_first(params, "acChargeGunConnectionState", "dcChargeGunConnectionState")),
            "acChargeCurrent": _as_float(_first(params, "BattACChrgInCurr", "battACChrgInCurr")),
            "dcChargeCurrent": _as_float(_first(params, "BattDCChrgInCurr", "battDCChrgInCurr")),
            "chargeCurrent": _as_float(_first(params, "BattACChrgInCurr", "BattDCChrgInCurr", "battACChrgInCurr", "battDCChrgInCurr")),
            "remainChargeTime": _as_int(params.get("chargDeltMins")),
            "dcChargeGunConnectStatus": _as_int(params.get("dcChargeGunConnectionState")),
            "chargeCoverStatus": _as_int(params.get("chargeCoverStatus")),
        },
        "door": {
            "doors": [
                _as_int(params.get("driverDoor")),
                _as_int(params.get("passengerDoor")),
                _as_int(params.get("leftRearDoor")),
                _as_int(params.get("rightRearDoor")),
            ],
            "trunk": _as_int(params.get("trunk")),
            "hood": _as_int(params.get("hoodStatus")),
            "driverLock": _as_int(params.get("driverDoorLock")),
            "passengerLock": _as_int(params.get("passengerDoorLock")),
        },
        "window": {
            "windows": [
                _as_int(params.get("diverWindow")),
                _as_int(params.get("passengerWindow")),
                _as_int(params.get("leftRearWindow")),
                _as_int(params.get("rightRearWindow")),
            ],
            "degrees": [
                _as_int(params.get("leftAnteriorWindowDegree")),
                _as_int(params.get("rightAnteriorWindowDegree")),
                _as_int(params.get("leftRearWindowDegree")),
                _as_int(params.get("rightRearWindowDegree")),
            ],
            "sunroofDegree": _as_int(params.get("skyWindowDegree")),
        },
        "lamp": {
            "highBeam": _as_int(params.get("highBeam")),
            "lowBeam": _as_int(params.get("lowBeam")),
            "positionLamp": _as_int(params.get("positionLamp")),
            "frontFoglamp": _as_int(params.get("frontFoglamp")),
            "rearFoglamp": _as_int(params.get("rearFoglamp")),
            "leftTurn": _as_int(params.get("turnLndicatorLeft")),
            "rightTurn": _as_int(params.get("turnLndicatorRight")),
        },
        "tire": {
            "leftFront": {"pressure": _as_float(params.get("lfTyrePressure")), "warning": _as_int(params.get("lfPressureWarning"))},
            "rightFront": {"pressure": _as_float(params.get("rfTyrePressure")), "warning": _as_int(params.get("rfPressureWarning"))},
            "leftBack": {"pressure": _as_float(params.get("lrTyrePressure")), "warning": _as_int(params.get("lrPressureWarning"))},
            "rightBack": {"pressure": _as_float(params.get("rrTyrePressure")), "warning": _as_int(params.get("rrPressureWarning"))},
        },
        "seat": {
            "rightFront": {
                "level": _as_int(params.get("driverSeatHeatStatus")),
                "heatStatus": _as_int(params.get("driverSeatHeatStatus")),
                "ventStatus": _as_int(params.get("driverSeatAirStatus")),
            },
            "leftFront": {
                "level": _as_int(params.get("passengerSeatHeatStatus")),
                "heatStatus": _as_int(params.get("passengerSeatHeatStatus")),
                "ventStatus": _as_int(params.get("passengerSeatAirStatus")),
            },
        },
        "rawS05": params,
    }
    return condition


class DeepalApiError(Exception):
    """Base API error."""


class DeepalAuthError(DeepalApiError):
    """Authentication failed or token expired."""


class DeepalCommandAuthError(DeepalAuthError):
    """Remote command key/session authentication failed."""


class DeepalCommandNotReady(DeepalApiError):
    """Remote command prerequisites are missing."""


class DeepalRateLimitError(DeepalApiError):
    """The API rate-limited the request."""


@dataclass(slots=True)
class DeepalTokens:
    """Token bundle returned by the app login flow."""

    access_token: str
    refresh_token: str | None = None


class DeepalClient:
    """Minimal client for captured Deepal cloud endpoints."""

    def __init__(
        self,
        session: ClientSession,
        *,
        access_token: str,
        refresh_token: str | None,
        country: str,
        language: str,
        app_version: str,
        device_id: str,
        private_key_pem: str | None = None,
        enable_commands: bool = False,
        enable_api_logging: bool = False,
        rc_token: str | None = None,
        control_pin: str | None = None,
        cac_token: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self._session = session
        self.tokens = DeepalTokens(access_token, refresh_token)
        self.country = country
        region = REGION_GATEWAYS.get((country or "").upper())
        self.base_url = region["base_url"] if region else BASE_URL
        self.gateway_prefix = region["gateway"] if region else DEFAULT_GATEWAY
        self.language = language
        self.app_version = app_version
        self.device_id = device_id
        self.enable_commands = enable_commands
        self.enable_api_logging = enable_api_logging
        self.rc_token = rc_token
        self.control_pin = control_pin
        self.cac_token = cac_token
        self.user_id = user_id
        self._ca_tsp_token: str | None = None
        self._private_key = self._load_private_key(private_key_pem) if private_key_pem else None

    @property
    def commands_available(self) -> bool:
        """Return whether this client has enough material to call control endpoints."""
        return bool(self.enable_commands and self._private_key and (self.rc_token or self.control_pin))

    @property
    def commands_enabled(self) -> bool:
        """Return whether signed non-PIN remote commands can be sent."""
        return bool(self.enable_commands and self._private_key)

    @staticmethod
    def generate_login_keypair() -> tuple[str, str]:
        """Generate the RSA keypair used by the app login/signing flow."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        private_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        public_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        pub_body = "\n".join(
            line for line in public_pem.splitlines() if "BEGIN" not in line and "END" not in line
        ) + "\n"
        return private_pem, pub_body

    @staticmethod
    def _load_private_key(private_key_pem: str):
        return serialization.load_pem_private_key(private_key_pem.encode(), password=None)

    @staticmethod
    def encrypt_request_value(value: str) -> str:
        """Encrypt mobile/password/control-PIN values like the Android app."""
        public_key = serialization.load_der_public_key(base64.b64decode(REQUEST_ENCRYPTION_PUBLIC_KEY))
        ciphertext = public_key.encrypt(value.encode(), padding.PKCS1v15())
        return base64.b64encode(ciphertext).decode()

    def _headers(self, *, include_auth: bool = True, tsp_token: str | None = None) -> dict[str, str]:
        headers = {
            "authorization": "",
            "appid": "ca",
            "language": self.language,
            "appversion": self.app_version,
            "apptype": "Android",
            "devicetype": "samsung",
            "deviceid": self.device_id,
            "selectcountry": self.country,
            "x-os-version": "9",
            "accept-language": self.language,
            "content-type": "application/json; charset=UTF-8",
            "user-agent": "okhttp/4.12.0",
        }
        if include_auth and self.tokens.access_token:
            headers["authorization"] = (
                f"{self.tokens.access_token}|{self.cac_token}"
                if self.cac_token and "|" not in self.tokens.access_token
                else self.tokens.access_token
            )
            # CA/VOT endpoints such as getConnConf reject requests with
            # "X-Tsp-User-Token is empty" unless a user token is also sent here.
            user_token = tsp_token or self.tokens.access_token.partition("|")[0]
            headers["X-Tsp-User-Token"] = user_token
            headers["X-VCS-User-Token"] = user_token
        return headers

    async def _post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        include_auth: bool = True,
        base_url: str | None = None,
        tsp_token: str | None = None,
        retry: bool = True,
    ) -> Any:
        """POST one request, retrying transient network failures when ``retry`` is set.

        Physical vehicle commands must pass ``retry=False``: a request that timed
        out may still have reached the car, and resending it repeats the action.
        """
        if base_url is None:
            base_url = self.base_url
        if self.gateway_prefix != DEFAULT_GATEWAY and path.startswith(DEFAULT_GATEWAY + "/"):
            path = self.gateway_prefix + path[len(DEFAULT_GATEWAY):]
        url = f"{base_url}{path}"
        headers = self._headers(include_auth=include_auth, tsp_token=tsp_token)
        started_at = time.monotonic()
        request_payload = payload or {}
        if self.enable_api_logging:
            _LOGGER.warning(
                "Deepal API debug request path=%s headers=%s payload=%s",
                path,
                _safe_log_headers(headers),
                _redact_for_log(request_payload),
            )
        request_body = json.dumps(request_payload, separators=(",", ":"), ensure_ascii=False)
        attempts = _TRANSIENT_RETRY_ATTEMPTS if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                async with self._session.post(
                    url,
                    data=request_body,
                    headers=headers,
                    timeout=30,
                ) as resp:
                    status = resp.status
                    resp.raise_for_status()
                    body = await resp.json(content_type=None)
                break
            except ClientResponseError as err:
                if self.enable_api_logging:
                    _LOGGER.warning(
                        "Deepal API debug HTTP error path=%s status=%s elapsed=%.3fs error=%s",
                        path,
                        err.status,
                        time.monotonic() - started_at,
                        type(err).__name__,
                    )
                if err.status in (401, 403):
                    raise DeepalAuthError(f"Deepal auth failed: HTTP {err.status}") from err
                raise DeepalApiError(f"Deepal HTTP error {err.status} for {path}") from err
            except (ClientError, TimeoutError) as err:
                if self.enable_api_logging:
                    _LOGGER.warning(
                        "Deepal API debug request error path=%s attempt=%s elapsed=%.3fs error=%s",
                        path,
                        attempt,
                        time.monotonic() - started_at,
                        type(err).__name__,
                    )
                if attempt >= attempts:
                    raise DeepalApiError(f"Deepal request failed for {path}: {err}") from err
                await asyncio.sleep(_TRANSIENT_RETRY_DELAY)

        if self.enable_api_logging:
            _LOGGER.warning(
                "Deepal API debug response path=%s status=%s elapsed=%.3fs body=%s",
                path,
                status,
                time.monotonic() - started_at,
                _redact_for_log(body),
            )

        if not isinstance(body, dict):
            raise DeepalApiError(f"Unexpected Deepal response for {path}: {type(body).__name__}")
        if body.get("success") is False:
            code = body.get("code")
            msg = body.get("msg")
            if str(code) == "CAC_1_1_01_033":
                raise DeepalRateLimitError(f"Deepal rate limit: {code} {msg}")
            if path.endswith("/serial-no/get") and str(code) == "COMMON_1_1_01_001":
                raise DeepalCommandAuthError(
                    "Deepal remote command signing was rejected. Reauthenticate the integration to register a new command key."
                )
            if code and (
                "AUTH" in str(code).upper()
                or str(code).startswith("401")
                or str(code) == "APP_1_1_02_004"
                or str(code) == "APP_1_1_02_005"
            ):
                raise DeepalAuthError(f"Deepal auth failed: {code} {msg}")
            raise DeepalApiError(f"Deepal API error for {path}: {code} {msg}")
        return body.get("data")

    async def _post_ca(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        """POST to the CA gateway used by S05 MQTT setup endpoints.

        Accounts differ in which token the gateway accepts as the TSP user token,
        so fall back to the cacToken once and remember whichever works.
        """
        try:
            return await self._post(path, payload, base_url=CA_BASE_URL, tsp_token=self._ca_tsp_token)
        except DeepalApiError as err:
            if self._ca_tsp_token or not self.cac_token or "APIGW_" not in str(err):
                raise
            data = await self._post(path, payload, base_url=CA_BASE_URL, tsp_token=self.cac_token)
            self._ca_tsp_token = self.cac_token
            return data

    async def refresh_tokens(self) -> DeepalTokens:
        """Refresh the bearer token using the captured app endpoint."""
        if not self.tokens.refresh_token:
            raise DeepalAuthError("No refresh token is available")
        data = await self._post(
            "/intl-app-gw/intl-app-auth/api/auth/refresh-token",
            {"refreshToken": self.tokens.refresh_token},
        )
        if not isinstance(data, dict) or not data.get("token"):
            raise DeepalAuthError("Refresh response did not include a token")
        self.tokens = DeepalTokens(str(data["token"]), data.get("refreshToken") or self.tokens.refresh_token)
        if data.get("cacToken"):
            self.cac_token = data.get("cacToken")
        self._ca_tsp_token = None
        return self.tokens

    async def send_auth_code(self, *, country_code: str, mobile: str) -> None:
        """Request an SMS login code."""
        await self._post(
            "/intl-app-gw/intl-app-auth/api/login/send-auth-code",
            {
                "countryCode": country_code,
                "mobile": self.encrypt_request_value(mobile),
            },
            include_auth=False,
        )

    async def send_email_auth_code(self, *, email: str) -> None:
        """Request an email login code."""
        await self._post(
            "/intl-app-gw/intl-app-auth/api/login/email-send-auth-code",
            {
                "type": "0",
                "email": self.encrypt_request_value(email),
            },
            include_auth=False,
        )

    async def login_by_mobile_code(
        self,
        *,
        country_code: str,
        mobile: str,
        auth_code: str,
        sales_country: str,
        pub_key: str,
    ) -> dict[str, Any]:
        """Login using SMS code and a generated command-signing public key."""
        data = await self._post(
            "/intl-app-gw/intl-app-auth/api/login/login-by-mobile-code",
            {
                "authCode": auth_code,
                "countryCode": country_code,
                "mobile": self.encrypt_request_value(mobile),
                "salesCountry": sales_country,
                "pubKey": pub_key,
            },
            include_auth=False,
        )
        if not isinstance(data, dict) or not data.get("token"):
            raise DeepalAuthError("Login response did not include a token")
        self.tokens = DeepalTokens(str(data["token"]), data.get("refreshToken"))
        self.cac_token = data.get("cacToken")
        return data

    async def login_by_email_code(
        self,
        *,
        email: str,
        auth_code: str,
        sales_country: str,
        pub_key: str,
    ) -> dict[str, Any]:
        """Login using an email verification code and a generated command-signing public key."""
        data = await self._post(
            "/intl-app-gw/intl-app-auth/api/login/email-code-in",
            {
                "authCode": auth_code,
                "salesCountry": sales_country,
                "email": self.encrypt_request_value(email),
                "pubKey": pub_key,
            },
            include_auth=False,
        )
        if not isinstance(data, dict) or not data.get("token"):
            raise DeepalAuthError("Login response did not include a token")
        self.tokens = DeepalTokens(str(data["token"]), data.get("refreshToken"))
        self.cac_token = data.get("cacToken")
        return data

    async def vehicles(self) -> list[dict[str, Any]]:
        data = await self._post("/intl-app-gw/intl-app-user/api/car/vehicles")
        return data if isinstance(data, list) else []

    async def condition(self, vehicle_id: str) -> dict[str, Any]:
        payload = {
            "vechileCriteria": {
                "seat": "1",
                "door": "1",
                "hvac": "1",
                "charge": "1",
                "lamp": "1",
                "window": "1",
                "tire": "1",
                "vehicleStatus": "1",
            },
            "vehicleId": vehicle_id,
        }
        data = await self._post("/intl-app-gw/intl-app-car-condition/api/vehicle/condition", payload)
        return data if isinstance(data, dict) else {}

    async def s05_mqtt_condition(self, vehicle_id: str) -> dict[str, Any]:
        """Fetch and decrypt S05 condition data from the MQTT telemetry path."""
        if not self.user_id:
            raise DeepalApiError("S05 MQTT telemetry requires user id; reauthenticate the integration")

        config = await self._s05_mqtt_config(vehicle_id)
        token = await self._s05_mqtt_token()
        condition = await self._s05_mqtt_read_condition(config, token)
        if not condition:
            raise DeepalApiError("S05 MQTT telemetry did not return vehicle condition")
        return condition

    async def _s05_mqtt_config(self, vehicle_id: str) -> dict[str, Any]:
        data = await self._post_ca(
            "/user-apigw/vot-connect-conf-center/api/device/getConnConf",
            {
                "deviceId": self.device_id,
                "carId": vehicle_id,
                "deviceType": 1,
                "confTimestamp": 0,
                "deviceTimestamp": str(int(time.time() * 1000)),
            },
        )
        if not isinstance(data, dict):
            raise DeepalApiError("Unexpected S05 MQTT config response")
        return data

    async def _s05_mqtt_token(self) -> str:
        data = await self._post_ca(
            "/user-apigw/vot-connect-auth-center/api/auth/getAuthTokenByUserId",
            {"userId": self.user_id},
        )
        if not isinstance(data, dict) or not data.get("authToken"):
            raise DeepalApiError("S05 MQTT auth response did not include authToken")
        return str(data["authToken"])

    async def s05_control_doors(self, *, vehicle_id: str, open_value: bool) -> str:
        """Lock or unlock an MQTT-backed vehicle."""
        return await self._s05_mqtt_command(
            vehicle_id,
            service_code="Door_Lock",
            method="Cnr_RR_ObjDrv",
            params={"MotCtrl": _S05_UNLOCK if open_value else _S05_LOCK},
        )

    async def s05_control_windows(self, *, vehicle_id: str, open_value: bool) -> str:
        """Open or close all windows on an MQTT-backed vehicle."""
        return await self._s05_mqtt_command(
            vehicle_id,
            service_code="CarWin",
            method="Cnr_WinAllCtrl",
            params={"MotCtrlPos": _S05_POSITION_OPEN if open_value else _S05_POSITION_CLOSED},
        )

    async def s05_control_trunk(self, *, vehicle_id: str, open_value: bool) -> str:
        """Open or close the boot on an MQTT-backed vehicle."""
        return await self._s05_mqtt_command(
            vehicle_id,
            service_code="TailGateDrv",
            method="Cnr_TailGateDrv_ReqSt",
            params={
                "ReqTypeDoor": _S05_OPEN if open_value else _S05_CLOSE,
                "TarPosnPerc": _S05_POSITION_OPEN if open_value else _S05_POSITION_CLOSED,
            },
        )

    async def _s05_mqtt_command(
        self,
        vehicle_id: str,
        *,
        service_code: str,
        method: str,
        params: dict[str, Any],
    ) -> str:
        """Wake the S05, publish one command, and wait for its correlated response.

        The physical command is published exactly once: a lost acknowledgement
        must never cause a second physical action.
        """
        if not self.commands_available:
            raise DeepalCommandNotReady("Remote commands require explicit enablement and a control PIN")
        if self.control_pin:
            await self.check_control_code(self.control_pin)

        config = await self._s05_mqtt_config(vehicle_id)
        token = await self._s05_mqtt_token()
        info = ((config.get("mqttConnectionInfos") or [None])[0]) or {}
        cluster = ((info.get("clusterInfos") or [None])[0]) or {}
        host = str(cluster.get("brokerUrl", "")).replace("ssl://", "")
        port = int(cluster.get("brokerPort") or 8883)
        topics: list[str] = []
        login_pub_topic: str | None = None
        login_did: str | None = None
        command_pub_topic: str | None = None
        command_res_topic: str | None = None
        device_did: str | None = None

        for topic_info in info.get("topicInfos") or []:
            msg_type = topic_info.get("msgType")
            for topic in topic_info.get("pubTopics") or []:
                if msg_type == "loginout" and "/loginout/req" in topic:
                    login_pub_topic = topic
                    login_did = self._s05_topic_did(topic)
                elif msg_type == "commands" and "/commands/req" in topic:
                    command_pub_topic = topic
                    device_did = self._s05_topic_did(topic)
            for topic in topic_info.get("subTopics") or []:
                if msg_type in ("loginout", "commands"):
                    topics.append(topic)
                if msg_type == "commands" and "/commands/res" in topic:
                    command_res_topic = topic
                    if device_did is None:
                        device_did = self._s05_topic_did(topic)

        if not all((host, login_pub_topic, login_did, command_pub_topic, command_res_topic, device_did)):
            raise DeepalApiError("S05 MQTT config did not include command topics")

        context = await asyncio.to_thread(ssl.create_default_context)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context, server_hostname=host),
            timeout=15,
        )
        try:
            writer.write(_mqtt_connect_packet(login_did, login_did, token))
            await writer.drain()
            first, body = await asyncio.wait_for(_mqtt_read_packet(reader), timeout=15)
            rc = body[1] if first == 0x20 and len(body) >= 2 else None
            if rc != 0:
                raise DeepalApiError(f"S05 MQTT broker rejected connection: rc={rc}")

            writer.write(_mqtt_subscribe_packet(1, sorted(set(topics))))
            await writer.drain()
            await asyncio.wait_for(_mqtt_read_packet(reader), timeout=15)

            writer.write(_mqtt_publish_packet(login_pub_topic, self._s05_login_payload(login_did)))
            await writer.drain()

            wake_req_id: str | None = None
            command_req_id: str | None = None
            secret_key: str | None = None
            deadline = time.monotonic() + _S05_COMMAND_TIMEOUT
            while time.monotonic() < deadline:
                first, body = await asyncio.wait_for(
                    _mqtt_read_packet(reader), timeout=max(1, deadline - time.monotonic())
                )
                if first >> 4 != 3:
                    continue
                topic, payload, packet_id = _mqtt_parse_publish(first, body)
                if packet_id is not None:
                    writer.write(bytes([0x40, 0x02]) + struct.pack("!H", packet_id))
                    await writer.drain()

                if secret_key is None:
                    secret_key = self._s05_secret_from_payload(payload)
                    if secret_key:
                        wake_req_id = self._s05_req_id(device_did)
                        writer.write(
                            _mqtt_publish_packet(
                                command_pub_topic,
                                self._s05_command_request_payload(
                                    device_did,
                                    login_did,
                                    secret_key,
                                    wake_req_id,
                                    service_code="TxWakeup",
                                    method="Cnr_ReWakeup",
                                    params={},
                                ),
                            )
                        )
                        await writer.drain()
                    continue

                if topic != command_res_topic:
                    continue
                response_req_id = _s05_payload_req_id(payload)
                if response_req_id == wake_req_id and command_req_id is None:
                    # An already-awake car may reject the redundant wake; either
                    # response proves it was processed, so send the command once.
                    await asyncio.sleep(_S05_WAKE_SETTLE_SECONDS)
                    command_req_id = self._s05_req_id(device_did)
                    writer.write(
                        _mqtt_publish_packet(
                            command_pub_topic,
                            self._s05_command_request_payload(
                                device_did,
                                login_did,
                                secret_key,
                                command_req_id,
                                service_code=service_code,
                                method=method,
                                params=params,
                            ),
                        )
                    )
                    await writer.drain()
                    continue

                if command_req_id is None or response_req_id != command_req_id:
                    continue
                items = _s05_command_response_items(payload, secret_key, command_req_id)
                if self.enable_api_logging:
                    _LOGGER.warning(
                        "Deepal MQTT debug command service=%s method=%s params=%s response=%s",
                        service_code,
                        method,
                        _redact_for_log(params),
                        _redact_for_log(items),
                    )
                error = _s05_command_result_error(items, payload)
                if error:
                    raise DeepalApiError(f"S05 command failed: {error}")
                return command_req_id

            raise DeepalApiError("S05 MQTT command timed out without a correlated response")
        except TimeoutError as err:
            raise DeepalApiError("S05 MQTT command timed out without a correlated response") from err
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, TimeoutError, ssl.SSLError):
                pass

    async def _s05_mqtt_read_condition(self, config: dict[str, Any], token: str) -> dict[str, Any]:
        info = ((config.get("mqttConnectionInfos") or [None])[0]) or {}
        cluster = ((info.get("clusterInfos") or [None])[0]) or {}
        host = str(cluster.get("brokerUrl", "")).replace("ssl://", "")
        port = int(cluster.get("brokerPort") or 8883)
        topics: list[str] = []
        login_pub_topic: str | None = None
        login_did: str | None = None
        properties_get_topic: str | None = None
        device_did: str | None = None

        for topic_info in info.get("topicInfos") or []:
            msg_type = topic_info.get("msgType")
            for topic in topic_info.get("pubTopics") or []:
                if msg_type == "loginout" and "/loginout/req" in topic:
                    login_pub_topic = topic
                    login_did = self._s05_topic_did(topic)
                if msg_type == "properties" and "/properties/get/req" in topic:
                    properties_get_topic = topic
                    device_did = self._s05_topic_did(topic)
            for topic in topic_info.get("subTopics") or []:
                if "/commands/" not in topic and "/set/" not in topic:
                    topics.append(topic)
                if device_did is None and "/properties/" in topic:
                    device_did = self._s05_topic_did(topic)

        if not host or not login_pub_topic or not login_did or not properties_get_topic or not device_did:
            raise DeepalApiError("S05 MQTT config did not include required topics")

        # Loading the system trust store is blocking I/O; keep it off the event loop.
        context = await asyncio.to_thread(ssl.create_default_context)
        reader: asyncio.StreamReader
        writer: asyncio.StreamWriter
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context, server_hostname=host),
            timeout=15,
        )
        try:
            writer.write(_mqtt_connect_packet(login_did, login_did, token))
            await writer.drain()
            first, body = await asyncio.wait_for(_mqtt_read_packet(reader), timeout=15)
            rc = body[1] if first == 0x20 and len(body) >= 2 else None
            if rc != 0:
                raise DeepalApiError(f"S05 MQTT broker rejected connection: rc={rc}")

            writer.write(_mqtt_subscribe_packet(1, sorted(set(topics))))
            await writer.drain()
            await asyncio.wait_for(_mqtt_read_packet(reader), timeout=15)

            writer.write(_mqtt_publish_packet(login_pub_topic, self._s05_login_payload(login_did)))
            await writer.drain()

            secret_key: str | None = None
            partial_params: dict[str, Any] = {}
            deadline = time.monotonic() + 18
            requested_condition = False

            while time.monotonic() < deadline:
                first, body = await asyncio.wait_for(_mqtt_read_packet(reader), timeout=max(1, deadline - time.monotonic()))
                if first >> 4 != 3:
                    continue
                topic, payload, packet_id = _mqtt_parse_publish(first, body)
                if packet_id is not None:
                    writer.write(bytes([0x40, 0x02]) + struct.pack("!H", packet_id))
                    await writer.drain()

                if not secret_key:
                    secret_key = self._s05_secret_from_payload(payload)
                    if secret_key:
                        condition_req_id = self._s05_req_id(device_did)
                        writer.write(
                            _mqtt_publish_packet(
                                properties_get_topic,
                                self._s05_condition_request_payload(device_did, login_did, secret_key, condition_req_id),
                            )
                        )
                        await writer.drain()
                        requested_condition = True
                    continue

                params = self._s05_condition_params_from_payload(payload, secret_key)
                if not params:
                    continue
                if topic.endswith("/properties/get/res") and len(params) > 10:
                    return _s05_condition_from_params(params)
                partial_params.update(params)
                if requested_condition and len(partial_params) > 30:
                    return _s05_condition_from_params(partial_params)

            return _s05_condition_from_params(partial_params) if partial_params else {}
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, TimeoutError, ssl.SSLError):
                pass

    @staticmethod
    def _s05_topic_did(topic: str) -> str | None:
        parts = topic.split("/")
        return parts[1] if len(parts) > 2 and parts[0] == "$vdp" else None

    @staticmethod
    def _s05_req_id(device_id: str) -> str:
        return f"{device_id}_{int(time.time() * 1000000)}"

    @classmethod
    def _s05_login_payload(cls, login_did: str) -> dict[str, Any]:
        return {
            "did": login_did,
            "r": cls._s05_req_id(login_did),
            "v": "v1.0.0",
            "mt": "loginout",
            "z": "unzip",
            "a": 0,
            "e": 0,
            "tf": 0,
            "dt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "pl": True,
            "sers": [
                {
                    "service_code": "login",
                    "params": {
                        "encryptEnable": 1,
                        "zipType": "gzip",
                        "ts": int(time.time() * 1000),
                    },
                }
            ],
        }

    @staticmethod
    def _s05_command_request_payload(
        device_did: str,
        login_did: str,
        secret_key: str,
        req_id: str,
        *,
        service_code: str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the encrypted VDP command envelope used by MQTT vehicles."""
        sers = [{"service_code": service_code, "command_code": method, "params": params}]
        return {
            "did": device_did,
            "r": req_id,
            "v": "v1.0.0",
            "mt": "commands",
            "e": 1,
            "z": "gzip",
            "tf": 0,
            "dt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "b": {"ruid": login_did},
            "sers": _s05_aes_encrypt(sers, secret_key, req_id),
            "rt": "",
        }

    @staticmethod
    def _s05_secret_from_payload(payload: dict[str, Any]) -> str | None:
        for item in payload.get("rs") or []:
            if not isinstance(item, dict):
                continue
            for key in ("params", "data"):
                value = item.get(key)
                if isinstance(value, dict) and value.get("secretKey"):
                    return str(value["secretKey"])
        return None

    @staticmethod
    def _s05_condition_request_payload(device_did: str, login_did: str, secret_key: str, req_id: str) -> dict[str, Any]:
        sers = [
            {
                "service_code": "car_condition",
                "params": {"fetchPropertyType": 0},
            }
        ]
        return {
            "did": device_did,
            "r": req_id,
            "v": "v1.0.0",
            "mt": "properties",
            "e": 1,
            "z": "gzip",
            "tf": 0,
            "dt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "b": {"ruid": login_did},
            "sers": _s05_aes_encrypt(sers, secret_key, req_id),
            "rt": "",
        }

    @staticmethod
    def _s05_condition_params_from_payload(payload: dict[str, Any], secret_key: str) -> dict[str, Any]:
        req_id = payload.get("r")
        if not isinstance(req_id, str):
            return {}
        params: dict[str, Any] = {}
        for field in ("rs", "sers"):
            encrypted = payload.get(field)
            if not isinstance(encrypted, str) or not encrypted:
                continue
            try:
                items = _s05_aes_decrypt(encrypted, secret_key, req_id)
            except (ValueError, json.JSONDecodeError, gzip.BadGzipFile) as err:
                _LOGGER.debug("Failed to decrypt S05 MQTT %s payload: %s", field, err)
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                service_code = item.get("service_code")
                item_params = item.get("params")
                if service_code in (None, "car_condition", "BDC_Service", "BMS_Service", "OBC_Service", "THU_Service") and isinstance(item_params, dict):
                    params.update(item_params)
        return params

    def decrypt_seriral_no(self, serial_data: str) -> str:
        """Decrypt /serial-no/get response into the misspelled seriralNo field."""
        if self._private_key is None:
            raise DeepalCommandNotReady("Private key is required to decrypt serial number")
        ciphertext = base64.b64decode("".join(serial_data.split()))
        plaintext = self._private_key.decrypt(ciphertext, padding.PKCS1v15())
        return plaintext.decode().strip()

    def sign_payload(self, payload: dict[str, Any], *, omit_keys: set[str] | None = None) -> str:
        """Create the app-compatible RSA signature for a signed command payload."""
        if self._private_key is None:
            raise DeepalCommandNotReady("Private key is required to sign command request")
        omit_keys = omit_keys or set()
        parts = []
        for key in sorted(payload):
            if key == "sign" or key in omit_keys:
                continue
            value = payload[key]
            if isinstance(value, bool):
                value = str(value).lower()
            parts.append(f"{key}={value}")
        canonical = "&".join(parts)
        signature = self._private_key.sign(canonical.encode(), padding.PKCS1v15(), hashes.SHA256())
        return base64.encodebytes(signature).decode()

    async def get_serial_data(self, serial_type: str = "1") -> str:
        data = await self._post("/intl-app-gw/intl-app-car-control/api/serial-no/get", {"type": serial_type})
        if not isinstance(data, str):
            raise DeepalApiError("Unexpected serial-no response")
        return data

    async def get_security_code_status(self) -> dict[str, Any]:
        """Return the control-PIN status; the app always calls this before check-code."""
        data = await self._post("/intl-app-gw/intl-app-car-control/api/security-code/get-status")
        return data if isinstance(data, dict) else {}

    async def check_control_code(self, safe_code: str) -> str:
        """Exchange the remote-control PIN for an rcToken."""
        status = await self.get_security_code_status()
        remaining = _as_int(status.get("retryQuantity"))
        if remaining is not None and remaining <= 0:
            raise DeepalRateLimitError(
                "No control PIN attempts left; wait for the lockout to expire or reset the PIN in the Deepal app"
            )
        data = await self._post(
            "/intl-app-gw/intl-app-car-control/api/security-code/check-code",
            {"safeCode": self.encrypt_request_value(safe_code)},
        )
        if not isinstance(data, dict) or not data.get("rcToken"):
            raise DeepalAuthError("Control-code check did not return rcToken")
        self.rc_token = str(data["rcToken"])
        return self.rc_token

    async def _signed_command(
        self,
        *,
        path: str,
        vehicle_id: str,
        payload: dict[str, Any],
        serial_type: str = "1",
        require_rc_token: bool = False,
        sign_omit_keys: set[str] | None = None,
    ) -> str:
        """Send one app-style signed command and return its command id."""
        if not self.commands_enabled:
            raise DeepalCommandNotReady("Remote commands are not enabled or command signing is incomplete")
        reused_rc_token = False
        if require_rc_token:
            if self.rc_token:
                reused_rc_token = True
            elif self.control_pin:
                await self.check_control_code(self.control_pin)
            else:
                raise DeepalCommandNotReady("Control PIN or rcToken is required")
        serial_data = await self.get_serial_data(serial_type)
        seriral_no = self.decrypt_seriral_no(serial_data)
        signed_payload = {
            **payload,
            "rcToken": self.rc_token or "",
            "seriralNo": seriral_no,
            "vehicleId": vehicle_id,
        }
        signed_payload["sign"] = self.sign_payload(signed_payload, omit_keys=sign_omit_keys)
        try:
            data = await self._post(path, signed_payload, retry=False)
        except DeepalRateLimitError:
            raise
        except DeepalApiError:
            # A cached rcToken expires (the app asks for the PIN again roughly
            # weekly), and the server only reveals that by rejecting the command.
            if not (reused_rc_token and self.control_pin):
                raise
            self.rc_token = None
            await self.check_control_code(self.control_pin)
            signed_payload["rcToken"] = self.rc_token or ""
            signed_payload["sign"] = self.sign_payload(signed_payload, omit_keys=sign_omit_keys)
            data = await self._post(path, signed_payload, retry=False)
        if not isinstance(data, dict) or not data.get("commandId"):
            raise DeepalApiError("Control command did not return commandId")
        return str(data["commandId"])

    async def control_doors(self, *, vehicle_id: str, open_value: bool) -> str:
        """Send a lock/unlock command; never available unless explicitly enabled."""
        return await self._signed_command(
            path="/intl-app-gw/intl-app-car-control/api/control/doors",
            vehicle_id=vehicle_id,
            payload={"command": "lock", "open": open_value},
            require_rc_token=True,
            sign_omit_keys={"command"},
        )

    async def control_air_conditioner(
        self,
        *,
        vehicle_id: str,
        enabled: bool,
        target_temp_c: float,
        run_time: int = 30,
        wind_mode: int = 1,
    ) -> str:
        """Send the captured car-wide HVAC command."""
        return await self._signed_command(
            path="/intl-app-gw/intl-app-car-control/api/control/air-conditioner",
            vehicle_id=vehicle_id,
            payload={
                "command": "air",
                "enabled": enabled,
                "runTime": run_time,
                "targetTemp": int(round(target_temp_c * 10)),
                "windMode": wind_mode,
            },
            sign_omit_keys={"command", "rcToken"},
        )

    async def control_charge_limit(self, *, vehicle_id: str, percentage: int) -> str:
        """Set the maximum charge percentage."""
        return await self._signed_command(
            path="/intl-app-gw/intl-app-car-control/api/charge/percentage",
            vehicle_id=vehicle_id,
            payload={"chargePercentageMax": int(percentage), "command": "charge_max"},
            serial_type="2",
            sign_omit_keys={"command", "rcToken"},
        )

    async def control_charge_schedule(
        self,
        *,
        vehicle_id: str,
        plan_id: str,
        start_time: str,
        end_time: str,
        enabled: bool,
        plan_type: int = 1,
        time_format: int = 1,
        time_zone: str = "GMT+08:00",
    ) -> str:
        """Update the captured charging schedule plan."""
        switch = 1 if enabled else 0
        return await self._signed_command(
            path="/intl-app-gw/intl-app-car-control/api/charge/modify-plan",
            vehicle_id=vehicle_id,
            payload={
                "command": "modify-plan",
                "endSwitch": switch,
                "endTime": end_time,
                "planId": str(plan_id),
                "planType": plan_type,
                "startTime": start_time,
                "timeFormat": time_format,
                "timeZone": time_zone,
            },
            serial_type="2",
            sign_omit_keys={"command", "rcToken"},
        )

    async def control_windows(self, *, vehicle_id: str, open_value: bool, open_type: int = 10) -> str:
        """Open or close the windows using the captured all-window command."""
        return await self._signed_command(
            path="/intl-app-gw/intl-app-car-control/api/control/windows",
            vehicle_id=vehicle_id,
            payload={"command": "window", "open": open_value, "openType": open_type},
            require_rc_token=True,
            sign_omit_keys={"command"},
        )

    async def control_trunk(self, *, vehicle_id: str, open_value: bool) -> str:
        """Open or close the boot/trunk using the captured command."""
        return await self._signed_command(
            path="/intl-app-gw/intl-app-car-control/api/control/trunk",
            vehicle_id=vehicle_id,
            payload={"command": "trunk", "open": open_value},
            require_rc_token=True,
            sign_omit_keys={"command"},
        )

    async def control_flashing_honking(self, *, vehicle_id: str, action_type: int) -> str:
        """Trigger the captured flash/honk command.

        Captured action types: 1 flashes lights, 3 sounds the horn.
        """
        return await self._signed_command(
            path="/intl-app-gw/intl-app-car-control/api/control/flashing-honking",
            vehicle_id=vehicle_id,
            payload={"command": "flash_bee", "type": action_type},
            sign_omit_keys={"command", "rcToken"},
        )

    async def control_seats_heat(self, *, vehicle_id: str, driver: bool, level: int) -> str:
        """Set the driver or front-passenger seat heating level (0 turns it off)."""
        return await self._signed_command(
            path="/intl-app-gw/intl-app-car-control/api/control/seats/heat",
            vehicle_id=vehicle_id,
            payload=self._seat_payload("seats_heat", driver=driver, level=level),
            sign_omit_keys={"command", "rcToken"},
        )

    async def control_seats_wind(self, *, vehicle_id: str, driver: bool, level: int) -> str:
        """Set the driver or front-passenger seat ventilation level (0 turns it off)."""
        return await self._signed_command(
            path="/intl-app-gw/intl-app-car-control/api/control/seats/wind",
            vehicle_id=vehicle_id,
            payload=self._seat_payload("seats_wind", driver=driver, level=level),
            sign_omit_keys={"command", "rcToken"},
        )

    @staticmethod
    def _seat_payload(command: str, *, driver: bool, level: int) -> dict[str, Any]:
        # The endpoint rejects explicit nulls and a zero level, so only the
        # requested seat is sent and "off" is a zero switch without a level.
        prefix = "master" if driver else "copilot"
        payload: dict[str, Any] = {"command": command, f"{prefix}Switch": 1 if level > 0 else 0}
        if level > 0:
            payload[f"{prefix}Level"] = level
        return payload

    async def control_steering_wheel_heat(self, *, vehicle_id: str, open_value: bool) -> str:
        """Turn the steering wheel heating on or off."""
        return await self._signed_command(
            path="/intl-app-gw/intl-app-car-control/api/control/steering-wheel/heat",
            vehicle_id=vehicle_id,
            payload={"command": "steering_wheel_heating", "open": open_value},
            sign_omit_keys={"command", "rcToken"},
        )

    async def control_condition_inquiry(self, *, vehicle_id: str) -> str:
        """Ask the vehicle/cloud to fetch fresh condition data."""
        return await self._signed_command(
            path="/intl-app-gw/intl-app-car-control/api/control/condition-inquiry",
            vehicle_id=vehicle_id,
            payload={"command": "COMMAND_GET_NEW_CONDITION"},
            sign_omit_keys={"command", "rcToken"},
        )

    async def control_result(self, *, vehicle_id: str, command_id: str) -> dict[str, Any]:
        data = await self._post(
            "/intl-app-gw/intl-app-car-control/api/control/control-result",
            {"vehicleId": vehicle_id, "commandId": command_id},
        )
        return data if isinstance(data, dict) else {}
