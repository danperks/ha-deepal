"""Belgium login, S05 REST fallback, offline classification, and command wording."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[1])
if sys.path[0] != _ROOT:
    sys.path.insert(0, _ROOT)

from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import frame
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.deepal.api import (
    DeepalApiError,
    DeepalAuthError,
    DeepalTokens,
    _s05_command_result_error,
)
from custom_components.deepal.config_flow import COUNTRY_DIAL_CODES, COUNTRY_OPTIONS
from custom_components.deepal.const import DOMAIN, REGION_GATEWAYS
from custom_components.deepal.coordinator import DeepalDataUpdateCoordinator
from custom_components.deepal.error_codes import command_failure_message

_README = Path(__file__).resolve().parents[1] / "README.md"
_OFFLINE = (
    "Deepal API error for /intl-app-car-condition/api/vehicle/condition: "
    "HW_1_1_01_001 Operation failed"
)
_INVALID_TOKEN = (
    "Deepal API error for /user-apigw/vot-connect-conf-center/api/device/getConnConf: "
    "APIGW_-1_7_01_004 invalided token"
)


class BelgiumRegionTests(unittest.TestCase):
    def test_belgium_is_a_european_login_option(self) -> None:
        self.assertEqual(COUNTRY_OPTIONS["BE"], "Belgium (+32)")
        self.assertEqual(COUNTRY_DIAL_CODES["BE"], "32")
        self.assertNotIn("BE", REGION_GATEWAYS)
        readme = _README.read_text()
        self.assertIn("Belgium", readme)
        self.assertIn(
            "Netherlands-delivered vehicles are often registered under Belgium (`BE`)",
            readme,
        )


class CommandResultWordingTests(unittest.TestCase):
    def test_1032_says_the_car_is_powered_on_and_keeps_the_code(self) -> None:
        message = command_failure_message(1032, None)
        self.assertIn("1032", message)
        self.assertIn("car is powered on", message.lower())
        self.assertNotIn("None", message)

    def test_vehicle_message_wins_and_the_code_stays(self) -> None:
        message = command_failure_message(1032, "Operation failed. Power is not off.")
        self.assertIn("1032", message)
        self.assertIn("Power is not off.", message)
        self.assertNotIn("car is powered on", message.lower())

    def test_official_app_wording_fills_an_empty_message(self) -> None:
        message = command_failure_message("3006", "  ")
        self.assertIn("3006", message)
        self.assertIn("Previous Command is executing", message)
        cloud = command_failure_message("045", None)
        self.assertIn("045", cloud)
        self.assertIn("start the vehicle manually", cloud)

    def test_mqtt_result_uses_the_same_table(self) -> None:
        powered_on = _s05_command_result_error([{"resultCode": 1032}], {})
        self.assertIsNotNone(powered_on)
        assert powered_on is not None
        self.assertIn("1032", powered_on)
        self.assertIn("car is powered on", powered_on.lower())

        busy = _s05_command_result_error([{"resultCode": "3006"}], {})
        self.assertIsNotNone(busy)
        assert busy is not None
        self.assertIn("Previous Command is executing", busy)
        self.assertIn("(3006)", busy)

        own_words = _s05_command_result_error(
            [{"resultCode": 1032, "msg": "Operation failed. Power is not off."}],
            {},
        )
        self.assertEqual(own_words, "Operation failed. Power is not off. (1032)")

        machine = _s05_command_result_error([{"code": "VVCC_-1_-1_02_023"}], {})
        self.assertIsNotNone(machine)
        assert machine is not None
        self.assertIn("door status", machine)
        self.assertIn("VVCC_-1_-1_02_023", machine)

        preferred = _s05_command_result_error(
            [{"code": "VVCC_-1_-1_02_023", "msg": "left door open"}],
            {},
        )
        self.assertEqual(preferred, "left door open (VVCC_-1_-1_02_023)")

        self.assertEqual(_s05_command_result_error([{"resultCode": 9999}], {}), "result code 9999")
        self.assertEqual(_s05_command_result_error([{"resultCode": 39}], {}), "result code 39")
        self.assertIsNone(_s05_command_result_error([{"code": "000000"}], {}))


class _MqttClient:
    def __init__(self) -> None:
        self.commands_enabled = False
        self.mqtt_calls = 0
        self.rest_calls = 0
        self.refresh_calls = 0
        self.fail_mqtt = True
        self.auth_error = False
        self.mqtt_error = _INVALID_TOKEN
        self.rest_error: str | None = None

    async def vehicles(self) -> list[dict]:
        return [{"carId": "veh-1", "protocolType": "MQTT", "imgUrl": "https://cdn.example/a.png"}]

    async def s05_mqtt_condition(self, vehicle_id: str) -> dict:
        self.mqtt_calls += 1
        if self.auth_error:
            raise DeepalAuthError("expired token")
        if self.fail_mqtt:
            raise DeepalApiError(self.mqtt_error)
        return {"src": "mqtt", "vehicleStatus": {"soc": 80}}

    async def condition(self, vehicle_id: str) -> dict:
        self.rest_calls += 1
        if self.rest_error:
            raise DeepalApiError(self.rest_error)
        return {"src": "rest", "vehicleStatus": {"soc": 55}}

    async def refresh_tokens(self) -> DeepalTokens:
        self.refresh_calls += 1
        return DeepalTokens("new-access", "new-refresh")


class _HttpClient:
    def __init__(self, vehicles: list[list[dict]] | None = None) -> None:
        self.commands_enabled = False
        self.queue = vehicles or [[{"carId": "veh-1", "protocolType": "HTTP"}]]
        self.fetches = 0
        self.refresh_calls = 0
        self.authorize_calls = 0
        self.mode = "ok"
        self.result = {"resultCode": 0, "errorMsg": None}

    async def vehicles(self) -> list[dict]:
        self.fetches += 1
        if self.mode in {"auth-then-offline", "auth-then-auth", "refresh-fails", "refresh-api-fails"} and self.fetches == 1:
            raise DeepalAuthError("token expired")
        if self.mode == "auth-then-auth":
            raise DeepalAuthError("still expired")
        if len(self.queue) > 1:
            return self.queue.pop(0)
        return self.queue[0]

    async def condition(self, vehicle_id: str) -> dict:
        if self.mode in {"direct-offline", "auth-then-offline"}:
            raise DeepalApiError(_OFFLINE)
        return {"lastUpdatedAt": "same", "vehicleStatus": {"soc": 40}}

    async def refresh_tokens(self) -> DeepalTokens:
        self.refresh_calls += 1
        if self.mode in {"refresh-fails", "qr-refresh-fails"}:
            raise DeepalAuthError("refresh failed")
        if self.mode == "refresh-api-fails":
            raise DeepalApiError("refresh transport failed")
        return DeepalTokens("new-access", "new-refresh")

    async def authorize_qr_code(self, value: str) -> dict:
        self.authorize_calls += 1
        if self.mode == "qr-auth-then-offline" and self.authorize_calls == 1:
            raise DeepalAuthError("expired")
        if self.mode == "qr-auth-then-offline":
            raise DeepalApiError(_OFFLINE)
        if self.mode == "qr-auth-then-auth":
            raise DeepalAuthError("still expired")
        if self.mode == "qr-refresh-fails" and self.authorize_calls == 1:
            raise DeepalAuthError("expired")
        return {"qr_code": value, "confirm": "suc"}

    async def control_result(self, *, vehicle_id: str, command_id: str) -> dict:
        return self.result


def _loaded_entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry:
    entry = ConfigEntry(
        data={"access_token": "old-access", "refresh_token": "old-refresh"},
        discovery_keys={},
        domain=DOMAIN,
        entry_id=entry_id,
        minor_version=1,
        options={},
        source="user",
        title="Deepal",
        unique_id=entry_id,
        version=1,
        subentries_data=None,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    return entry


class ResilienceCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self._tmp.name)
        frame.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await ir.async_load(self.hass)

    async def asyncTearDown(self) -> None:
        await self.hass.async_stop()
        self._tmp.cleanup()

    def _coordinator(self, client: object, entry_id: str = "veh-entry") -> DeepalDataUpdateCoordinator:
        entry = _loaded_entry(self.hass, entry_id)
        return DeepalDataUpdateCoordinator(self.hass, entry, client, "veh-1")

    async def test_mqtt_api_error_falls_back_to_rest_and_backs_off(self) -> None:
        client = _MqttClient()
        coordinator = self._coordinator(client)
        with self.assertLogs("custom_components.deepal.coordinator", level="WARNING") as captured:
            vehicles, condition = await coordinator._async_fetch()
            vehicles_again, condition_again = await coordinator._async_fetch()
            await coordinator._async_fetch()
        self.assertEqual(condition["src"], "rest")
        self.assertEqual(condition_again["src"], "rest")
        self.assertEqual(vehicles[0]["carId"], "veh-1")
        self.assertEqual(client.mqtt_calls, 1)
        self.assertEqual(client.rest_calls, 3)
        self.assertEqual(coordinator._mqtt_failures, 1)
        self.assertGreater(coordinator._mqtt_retry_after, asyncio.get_running_loop().time())
        self.assertEqual(len(captured.records), 1)
        self.assertIsNone(captured.records[0].exc_info)
        self.assertNotIn("Traceback", captured.records[0].getMessage())
        self.assertIn("APIGW_-1_7_01_004", captured.records[0].getMessage())
        self.assertIn("REST condition", captured.records[0].getMessage())
        updated = await coordinator._async_update_data()
        self.assertEqual(updated["condition"]["src"], "rest")
        self.assertIsNone(coordinator.last_refresh_failure)
        self.assertEqual(client.mqtt_calls, 1)

    async def test_mqtt_auth_error_is_not_swallowed(self) -> None:
        client = _MqttClient()
        client.auth_error = True
        coordinator = self._coordinator(client)
        with self.assertRaises(DeepalAuthError):
            await coordinator._async_fetch()
        self.assertEqual(client.rest_calls, 0)
        self.assertEqual(coordinator._mqtt_failures, 0)
        self.assertEqual(coordinator._mqtt_retry_after, 0.0)

    async def test_mqtt_auth_refresh_retries_mqtt(self) -> None:
        client = _MqttClient()
        client.auth_error = True
        coordinator = self._coordinator(client)

        async def s05_mqtt_condition(vehicle_id: str) -> dict:
            client.mqtt_calls += 1
            if client.mqtt_calls == 1:
                raise DeepalAuthError("expired token")
            return {"src": "mqtt", "vehicleStatus": {"soc": 80}}

        client.s05_mqtt_condition = s05_mqtt_condition
        updated = await coordinator._async_update_data()
        self.assertEqual(updated["condition"]["src"], "mqtt")
        self.assertEqual(client.mqtt_calls, 2)
        self.assertEqual(client.rest_calls, 0)
        self.assertEqual(client.refresh_calls, 1)
        self.assertEqual(coordinator.entry.data["access_token"], "new-access")
        self.assertEqual(coordinator._mqtt_failures, 0)

    async def test_backoff_is_five_fifteen_then_thirty_minutes_and_then_recovers(self) -> None:
        client = _MqttClient()
        client.mqtt_error = "S05 MQTT broker timed out"
        coordinator = self._coordinator(client)
        delays: list[float] = []
        loop = asyncio.get_running_loop()
        for _ in range(4):
            before = loop.time()
            await coordinator._async_fetch()
            delays.append(coordinator._mqtt_retry_after - before)
            coordinator._mqtt_retry_after = 0
        self.assertEqual(len(delays), 4)
        for actual, expected in zip(delays, (300, 900, 1800, 1800), strict=True):
            self.assertAlmostEqual(actual, expected, delta=1)
        self.assertEqual(client.mqtt_calls, 4)
        self.assertEqual(client.rest_calls, 4)
        client.fail_mqtt = False
        with self.assertLogs("custom_components.deepal.coordinator", level="INFO") as captured:
            _, condition = await coordinator._async_fetch()
        self.assertEqual(condition["src"], "mqtt")
        self.assertEqual(coordinator._mqtt_failures, 0)
        self.assertEqual(coordinator._mqtt_retry_after, 0.0)
        self.assertTrue(any("working again" in line for line in captured.output))

    async def test_rest_fallback_failure_is_raised(self) -> None:
        client = _MqttClient()
        client.rest_error = "REST down"
        coordinator = self._coordinator(client)
        with self.assertRaises(DeepalApiError) as raised:
            await coordinator._async_fetch()
        self.assertIn("REST down", str(raised.exception))
        self.assertEqual(coordinator._mqtt_failures, 1)
        self.assertEqual(client.mqtt_calls, 1)
        self.assertEqual(client.rest_calls, 1)

    async def test_direct_offline_error_stays_update_failed(self) -> None:
        client = _HttpClient()
        client.mode = "direct-offline"
        coordinator = self._coordinator(client)
        with self.assertRaises(UpdateFailed) as raised:
            await coordinator._async_update_data()
        self.assertIn("HW_1_1_01_001", str(raised.exception))
        self.assertNotIsInstance(raised.exception, ConfigEntryAuthFailed)
        self.assertEqual(client.refresh_calls, 0)
        self.assertEqual(coordinator.entry.state, ConfigEntryState.LOADED)
        self.assertEqual(coordinator.entry.data["access_token"], "old-access")
        self.assertGreaterEqual(coordinator.refresh_failure_count, 1)

    async def test_offline_after_token_refresh_stays_update_failed(self) -> None:
        client = _HttpClient()
        client.mode = "auth-then-offline"
        coordinator = self._coordinator(client)
        with self.assertRaises(UpdateFailed) as raised:
            await coordinator._async_update_data()
        self.assertIn("HW_1_1_01_001", str(raised.exception))
        self.assertNotIsInstance(raised.exception, ConfigEntryAuthFailed)
        self.assertEqual(client.refresh_calls, 1)
        self.assertEqual(client.fetches, 2)
        self.assertEqual(coordinator.entry.data["access_token"], "new-access")
        self.assertEqual(coordinator.entry.state, ConfigEntryState.LOADED)
        self.assertIn("HW_1_1_01_001", coordinator.last_refresh_failure or "")

    async def test_retry_auth_error_and_refresh_failure_unload_the_session(self) -> None:
        client = _HttpClient()
        client.mode = "auth-then-auth"
        coordinator = self._coordinator(client, "auth-retry")
        with self.assertRaises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        self.assertEqual(client.refresh_calls, 1)
        self.assertEqual(coordinator.entry.data["access_token"], "new-access")

        client = _HttpClient()
        client.mode = "refresh-fails"
        coordinator = self._coordinator(client, "refresh-fails")
        with self.assertRaises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        self.assertEqual(coordinator.entry.data["access_token"], "old-access")

        client = _HttpClient()
        client.mode = "refresh-api-fails"
        coordinator = self._coordinator(client, "refresh-api-fails")
        with self.assertRaises(ConfigEntryAuthFailed) as raised:
            await coordinator._async_update_data()
        self.assertIn("refresh transport failed", str(raised.exception))
        self.assertEqual(coordinator.entry.data["access_token"], "old-access")

    async def test_qr_retry_offline_is_update_failed_and_auth_retry_is_not(self) -> None:
        client = _HttpClient()
        client.mode = "qr-auth-then-offline"
        coordinator = self._coordinator(client)

        async def unexpected_refresh() -> None:
            raise AssertionError("vehicle refresh should not run")

        coordinator.async_request_refresh = unexpected_refresh
        with self.assertRaises(UpdateFailed) as raised:
            await coordinator.async_authorize_qr_code("qr-token")
        self.assertIn("HW_1_1_01_001", str(raised.exception))
        self.assertEqual(client.refresh_calls, 1)
        self.assertEqual(client.authorize_calls, 2)
        self.assertEqual(coordinator.entry.data["access_token"], "new-access")

        client = _HttpClient()
        client.mode = "qr-auth-then-auth"
        coordinator = self._coordinator(client, "qr-auth-retry")
        coordinator.async_request_refresh = unexpected_refresh
        with self.assertRaises(ConfigEntryAuthFailed):
            await coordinator.async_authorize_qr_code("qr-token")
        self.assertEqual(client.authorize_calls, 2)

        client = _HttpClient()
        client.mode = "qr-refresh-fails"
        coordinator = self._coordinator(client, "qr-refresh-fails")
        coordinator.async_request_refresh = unexpected_refresh
        with self.assertRaises(ConfigEntryAuthFailed):
            await coordinator.async_authorize_qr_code("qr-token")
        self.assertEqual(client.refresh_calls, 1)
        self.assertEqual(client.authorize_calls, 1)
        self.assertEqual(coordinator.entry.data["access_token"], "old-access")

    async def test_img_url_is_kept_when_a_later_poll_omits_it(self) -> None:
        client = _HttpClient(
            [
                [{"carId": "veh-1", "protocolType": "HTTP", "imgUrl": ""}],
                [{"carId": "veh-1", "protocolType": "HTTP", "imgUrl": "https://cdn.example/a.png"}],
                [{"carId": "veh-1", "protocolType": "HTTP"}],
                [{"carId": "veh-1", "protocolType": "HTTP", "imgUrl": "   "}],
                [{"carId": "veh-1", "protocolType": "HTTP", "imgUrl": None}],
                [{"carId": "veh-1", "protocolType": "HTTP", "imgUrl": "https://cdn.example/b.png"}],
                [{"carId": "veh-1", "protocolType": "HTTP", "imgUrl": ""}],
            ]
        )
        coordinator = self._coordinator(client)
        blank = await coordinator._async_update_data()
        self.assertFalse(str(blank["vehicle"].get("imgUrl") or "").strip())

        first = await coordinator._async_update_data()
        self.assertEqual(first["vehicle"]["imgUrl"], "https://cdn.example/a.png")
        for _ in range(3):
            kept = await coordinator._async_update_data()
            self.assertEqual(kept["vehicle"]["imgUrl"], "https://cdn.example/a.png")
            self.assertEqual(kept["vehicles"][0]["imgUrl"], "https://cdn.example/a.png")

        replaced = await coordinator._async_update_data()
        self.assertEqual(replaced["vehicle"]["imgUrl"], "https://cdn.example/b.png")
        kept_new = await coordinator._async_update_data()
        self.assertEqual(kept_new["vehicle"]["imgUrl"], "https://cdn.example/b.png")

    async def test_poll_command_update_uses_result_code_wording(self) -> None:
        client = _HttpClient()
        coordinator = self._coordinator(client)
        client.result = {"resultCode": 1032, "errorMsg": None}
        with self.assertRaises(HomeAssistantError) as raised:
            await coordinator.async_poll_command_update("cmd-1", previous_last_updated="same", timeout=5, interval=0.01)
        self.assertIn("1032", str(raised.exception))
        self.assertIn("car is powered on", str(raised.exception).lower())

        client.result = {"resultCode": 1032, "errorMsg": "Operation failed. Power is not off."}
        with self.assertRaises(HomeAssistantError) as raised:
            await coordinator.async_poll_command_update("cmd-2", previous_last_updated="same", timeout=5, interval=0.01)
        self.assertIn("Power is not off.", str(raised.exception))
        self.assertIn("result code 1032", str(raised.exception))

        client.result = {"resultCode": "3006", "errorMsg": ""}
        with self.assertRaises(HomeAssistantError) as raised:
            await coordinator.async_poll_command_update("cmd-3", previous_last_updated="same", timeout=5, interval=0.01)
        self.assertIn("Previous Command is executing", str(raised.exception))

        client.result = {"resultCode": 1015, "errorMsg": None}
        await coordinator.async_poll_command_update(
            "cmd-4",
            previous_last_updated="same",
            is_done=lambda: False,
            timeout=0,
            interval=0.01,
        )


if __name__ == "__main__":
    unittest.main()
