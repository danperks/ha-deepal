"""Async client for the Changan Deepal cloud API."""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .const import BASE_URL, REQUEST_ENCRYPTION_PUBLIC_KEY

_LOGGER = logging.getLogger(__name__)

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
    ) -> None:
        self._session = session
        self.tokens = DeepalTokens(access_token, refresh_token)
        self.country = country
        self.language = language
        self.app_version = app_version
        self.device_id = device_id
        self.enable_commands = enable_commands
        self.enable_api_logging = enable_api_logging
        self.rc_token = rc_token
        self.control_pin = control_pin
        self.cac_token = cac_token
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

    def _headers(self, *, include_auth: bool = True) -> dict[str, str]:
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
        return headers

    async def _post(self, path: str, payload: dict[str, Any] | None = None, *, include_auth: bool = True) -> Any:
        url = f"{BASE_URL}{path}"
        headers = self._headers(include_auth=include_auth)
        started_at = time.monotonic()
        try:
            request_payload = payload or {}
            if self.enable_api_logging:
                _LOGGER.warning(
                    "Deepal API debug request path=%s headers=%s payload=%s",
                    path,
                    _safe_log_headers(headers),
                    _redact_for_log(request_payload),
                )
            request_body = json.dumps(request_payload, separators=(",", ":"), ensure_ascii=False)
            async with self._session.post(
                url,
                data=request_body,
                headers=headers,
                timeout=30,
            ) as resp:
                status = resp.status
                resp.raise_for_status()
                body = await resp.json(content_type=None)
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
                    "Deepal API debug request error path=%s elapsed=%.3fs error=%s",
                    path,
                    time.monotonic() - started_at,
                    type(err).__name__,
                )
            raise DeepalApiError(f"Deepal request failed for {path}: {err}") from err

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

    async def check_control_code(self, safe_code: str) -> str:
        """Exchange the remote-control PIN for an rcToken."""
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
        if require_rc_token and not self.rc_token:
            if not self.control_pin:
                raise DeepalCommandNotReady("Control PIN or rcToken is required")
            await self.check_control_code(self.control_pin)
        serial_data = await self.get_serial_data(serial_type)
        seriral_no = self.decrypt_seriral_no(serial_data)
        signed_payload = {
            **payload,
            "rcToken": self.rc_token or "",
            "seriralNo": seriral_no,
            "vehicleId": vehicle_id,
        }
        signed_payload["sign"] = self.sign_payload(signed_payload, omit_keys=sign_omit_keys)
        data = await self._post(path, signed_payload)
        if not isinstance(data, dict) or not data.get("commandId"):
            raise DeepalApiError("Control command did not return commandId")
        return str(data["commandId"])

    async def control_doors(self, *, vehicle_id: str, command: str, open_value: bool) -> str:
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
