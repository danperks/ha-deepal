"""QR parsing, tmp-auth posts, and log redaction."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = str(Path(__file__).resolve().parents[1])
if sys.path[0] != _ROOT:
    sys.path.insert(0, _ROOT)

from aiohttp import ClientError

from custom_components.deepal.api import (
    DeepalApiError,
    DeepalClient,
    _redact_for_log,
    extract_qr_code,
)

_SAMPLE_QR = "00000000-0000-4000-8000-000000000001"
_SCAN_URL = (
    "https://ca-m.iov.changanauto.com.de/intl-app-gw/intl-app-user/api/qr-code/tmp-auth/scan"
    f"?qrCode={_SAMPLE_QR}"
)


class _Response:
    def __init__(self, body: dict) -> None:
        self.status = 200
        self._body = body

    def raise_for_status(self) -> None:
        return None

    async def json(self, content_type: str | None = None) -> dict:
        return self._body

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _Session:
    def __init__(self, bodies: list[dict]) -> None:
        self.bodies = list(bodies)
        self.calls: list[dict] = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        body = self.bodies.pop(0) if self.bodies else {"success": False, "code": "EMPTY", "msg": "no body"}
        return _Response(body)


class _Boom:
    async def __aenter__(self):
        raise ClientError("down")

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FailSession:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls += 1
        return _Boom()


def _client(session, *, country: str = "GB", logging: bool = False) -> DeepalClient:
    return DeepalClient(
        session,
        access_token="access-secret",
        refresh_token="refresh-secret",
        country=country,
        language="en_US",
        app_version="V1.12.0",
        device_id="device-secret",
        enable_api_logging=logging,
    )


class ExtractQrCodeTests(unittest.TestCase):
    def test_query_url(self) -> None:
        self.assertEqual(extract_qr_code(f"  {_SCAN_URL}  "), _SAMPLE_QR)

    def test_fragment_url(self) -> None:
        url = f"https://ca-m.iov.changanauto.com.de/#/tmp-auth?qrCode={_SAMPLE_QR}"
        self.assertEqual(extract_qr_code(url), _SAMPLE_QR)

    def test_query_wins_over_fragment(self) -> None:
        url = f"https://example.test/scan?qrCode=from-query#/path?qrCode=from-fragment"
        self.assertEqual(extract_qr_code(url), "from-query")

    def test_bare_qr_code_parameter(self) -> None:
        self.assertEqual(extract_qr_code(f"qrCode={_SAMPLE_QR}&unused=1"), _SAMPLE_QR)

    def test_bare_token(self) -> None:
        self.assertEqual(extract_qr_code(f"\n{_SAMPLE_QR}\t"), _SAMPLE_QR)

    def test_rejects_blank_internal_whitespace_and_url_without_code(self) -> None:
        for value in ("", "   ", "not a token", "https://example.test/scan", "https://example.test/scan?qrCode="):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    extract_qr_code(value)


class AuthorizeQrCodeTests(unittest.IsolatedAsyncioTestCase):
    def _success_bodies(self) -> list[dict]:
        return [
            {
                "success": True,
                "code": "00000",
                "data": {"vehicleId": "veh-1", "phone": "phone-secret-99", "email": "email-secret@example.com"},
            },
            {"success": True, "code": "00000", "data": "suc"},
        ]

    async def test_posts_scan_then_confirm_without_retry(self) -> None:
        session = _Session(self._success_bodies())
        client = _client(session)
        result = await client.authorize_qr_code(_SCAN_URL)
        self.assertEqual(
            result,
            {
                "qr_code": _SAMPLE_QR,
                "scan": {"vehicleId": "veh-1", "phone": "phone-secret-99", "email": "email-secret@example.com"},
                "confirm": "suc",
            },
        )
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            session.calls[0]["url"],
            "https://m.iov.changanauto.com.de/intl-app-gw/intl-app-user/api/qr-code/tmp-auth/scan",
        )
        self.assertEqual(
            session.calls[1]["url"],
            "https://m.iov.changanauto.com.de/intl-app-gw/intl-app-user/api/qr-code/tmp-auth/confirm",
        )
        self.assertEqual(json.loads(session.calls[0]["data"]), {"qrCode": _SAMPLE_QR})
        self.assertEqual(json.loads(session.calls[1]["data"]), {"qrCode": _SAMPLE_QR})

    async def test_bare_token_and_au_gateway_prefix(self) -> None:
        session = _Session(self._success_bodies())
        client = _client(session, country="AU")
        result = await client.authorize_qr_code(_SAMPLE_QR)
        self.assertEqual(result["qr_code"], _SAMPLE_QR)
        self.assertEqual(
            session.calls[0]["url"],
            "https://m.iov.changanauto.sg/appgw/intl-app-user/api/qr-code/tmp-auth/scan",
        )
        self.assertEqual(
            session.calls[1]["url"],
            "https://m.iov.changanauto.sg/appgw/intl-app-user/api/qr-code/tmp-auth/confirm",
        )

    async def test_network_error_is_not_retried(self) -> None:
        session = _FailSession()
        client = _client(session)

        async def _no_sleep(_delay: float) -> None:
            return None

        with patch("custom_components.deepal.api.asyncio.sleep", _no_sleep):
            with self.assertRaises(DeepalApiError):
                await client.authorize_qr_code(_SAMPLE_QR)
        self.assertEqual(session.calls, 1)

    async def test_invalid_qr_does_not_post(self) -> None:
        session = _Session([])
        client = _client(session)
        with self.assertRaises(DeepalApiError):
            await client.authorize_qr_code("   ")
        self.assertEqual(session.calls, [])

    async def test_scan_failure_does_not_confirm(self) -> None:
        session = _Session([{"success": False, "code": "APP_1", "msg": "rejected"}])
        client = _client(session)
        with self.assertRaises(DeepalApiError):
            await client.authorize_qr_code(_SAMPLE_QR)
        self.assertEqual(len(session.calls), 1)
        self.assertIn("/tmp-auth/scan", session.calls[0]["url"])

    async def test_logs_redact_qr_phone_email_and_mobile(self) -> None:
        session = _Session(
            [
                {
                    "success": True,
                    "code": "00000",
                    "data": {
                        "vehicleId": "veh-1",
                        "phone": "phone-secret-99",
                        "email": "email-secret@example.com",
                        "mobile": "mobile-secret-77",
                        "qrCode": _SAMPLE_QR,
                    },
                },
                {"success": True, "code": "00000", "data": "suc"},
            ]
        )
        client = _client(session, logging=True)
        with self.assertLogs("custom_components.deepal.api", level="WARNING") as captured:
            await client.authorize_qr_code(_SCAN_URL)
        text = "\n".join(captured.output)
        self.assertIn("[redacted]", text)
        for secret in (_SAMPLE_QR, "phone-secret-99", "email-secret@example.com", "mobile-secret-77", "access-secret"):
            self.assertNotIn(secret, text)


class RedactTests(unittest.TestCase):
    def test_qr_phone_email_mobile_are_redacted(self) -> None:
        redacted = _redact_for_log(
            {
                "qrCode": _SAMPLE_QR,
                "phone": "phone-secret-99",
                "email": "email-secret@example.com",
                "mobile": "mobile-secret-77",
                "lat": "51.5007",
                "nested": [{"qrCode": _SAMPLE_QR, "lng": "0.0"}],
            }
        )
        self.assertEqual(redacted["qrCode"], "[redacted]")
        self.assertEqual(redacted["phone"], "[redacted]")
        self.assertEqual(redacted["email"], "[redacted]")
        self.assertEqual(redacted["mobile"], "[redacted]")
        self.assertEqual(redacted["lat"], "51.5007")
        self.assertEqual(redacted["nested"][0]["qrCode"], "[redacted]")
        self.assertEqual(redacted["nested"][0]["lng"], "0.0")


if __name__ == "__main__":
    unittest.main()
