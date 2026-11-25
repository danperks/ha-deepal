"""Repair, options, and service flows for location QR authorisation."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[1])
if sys.path[0] != _ROOT:
    sys.path.insert(0, _ROOT)

import voluptuous as vol

from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import frame
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.service import _SERVICES_SCHEMA
from homeassistant.util.yaml import load_yaml_dict

from custom_components.deepal import async_handle_authorize_qr, async_unload_entry
from custom_components.deepal.api import DeepalApiError, DeepalAuthError, DeepalTokens
from custom_components.deepal.config_flow import DeepalOptionsFlow
from custom_components.deepal.const import DOMAIN, SERVICE_AUTHORIZE_QR
from custom_components.deepal.coordinator import DeepalDataUpdateCoordinator
from custom_components.deepal.repairs import (
    DeepalCommandReauthRepairFlow,
    DeepalLocationPermissionRepairFlow,
    async_create_fix_flow,
)
from custom_components.deepal.__init__ import _async_register_authorize_qr_service

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "deepal"


class _QrCoordinator:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[str] = []
        self.fail_times = fail_times

    async def async_authorize_qr_code(self, value: str) -> dict:
        self.calls.append(value)
        if len(self.calls) <= self.fail_times:
            raise DeepalApiError("rejected")
        return {"qr_code": "ok", "scan": {}, "confirm": "suc"}


class _OptionsEntry:
    def __init__(self) -> None:
        self.data = {"scan_interval": 60, "control_pin": "1234", "enable_commands": False}
        self.options = {}
        self.runtime_data = _QrCoordinator()
        self.reauthed = False

    def async_start_reauth(self, hass) -> None:
        self.reauthed = True


def _schema_keys(schema: vol.Schema) -> list:
    keys = []
    for key in schema.schema:
        keys.append(getattr(key, "schema", key))
    return keys


class OptionsAndRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_options_authorises_qr_without_storing_it(self) -> None:
        entry = _OptionsEntry()
        flow = DeepalOptionsFlow(entry)
        flow.flow_id = "options-1"
        form = await flow.async_step_init(None)
        self.assertEqual(form["type"], "form")
        self.assertIn("qr_auth_url", _schema_keys(form["data_schema"]))

        entry.runtime_data = _QrCoordinator(fail_times=1)
        failed = await flow.async_step_init(
            {
                "scan_interval": 90,
                "enable_commands": False,
                "control_pin": "",
                "enable_api_logging": False,
                "qr_auth_url": "https://example.test/scan?qrCode=bad",
            }
        )
        self.assertEqual(failed["type"], "form")
        self.assertEqual(failed["errors"]["qr_auth_url"], "qr_auth_failed")
        self.assertEqual(entry.runtime_data.calls, ["https://example.test/scan?qrCode=bad"])

        saved = await flow.async_step_init(
            {
                "scan_interval": 90,
                "enable_commands": False,
                "control_pin": "",
                "qr_auth_url": "  ",
            }
        )
        self.assertEqual(saved["type"], "create_entry")
        self.assertNotIn("qr_auth_url", saved["data"])
        self.assertEqual(saved["data"]["scan_interval"], 90)
        self.assertEqual(len(entry.runtime_data.calls), 1)

        entry.runtime_data = _QrCoordinator()
        saved = await flow.async_step_init(
            {
                "scan_interval": 45,
                "enable_commands": True,
                "control_pin": "1234",
                "qr_auth_url": "token-ok",
            }
        )
        self.assertEqual(saved["type"], "create_entry")
        self.assertEqual(saved["data"]["control_pin"], "1234")
        self.assertNotIn("qr_auth_url", saved["data"])
        self.assertEqual(entry.runtime_data.calls, ["token-ok"])

    async def test_options_pin_error_and_reauth_skip_qr(self) -> None:
        entry = _OptionsEntry()
        flow = DeepalOptionsFlow(entry)
        flow.flow_id = "options-2"
        pin = await flow.async_step_init(
            {"enable_commands": True, "control_pin": "  ", "qr_auth_url": "token-ok"}
        )
        self.assertEqual(pin["errors"]["control_pin"], "pin_required")
        self.assertEqual(entry.runtime_data.calls, [])

        reauth = await flow.async_step_init({"reauthenticate": True, "qr_auth_url": "token-ok"})
        self.assertEqual(reauth["type"], "abort")
        self.assertEqual(reauth["reason"], "reauth_triggered")
        self.assertTrue(entry.reauthed)
        self.assertEqual(entry.runtime_data.calls, [])

    async def test_repair_flow_retries_with_a_new_url(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        hass = HomeAssistant(tmp.name)
        hass.config_entries = ConfigEntries(hass, {})
        try:
            await self._assert_repair_flow(hass)
        finally:
            await hass.async_stop()
            tmp.cleanup()

    async def _assert_repair_flow(self, hass: HomeAssistant) -> None:
        entry = _loaded_entry(hass, "entry-1")
        entry.runtime_data = _QrCoordinator(fail_times=1)

        other = await async_create_fix_flow(hass, "entry-1_command_reauth", None)
        self.assertIsInstance(other, DeepalCommandReauthRepairFlow)
        repair = await async_create_fix_flow(hass, "entry-1_location_permission", None)
        self.assertIsInstance(repair, DeepalLocationPermissionRepairFlow)
        repair.hass = hass
        repair.issue_id = "entry-1_location_permission"
        repair.flow_id = "repair-1"

        shown = await repair.async_step_init(None)
        self.assertEqual(shown["step_id"], "authorize")
        self.assertIn("qr_auth_url", _schema_keys(shown["data_schema"]))

        failed = await repair.async_step_authorize({"qr_auth_url": "https://example.test/?qrCode=old"})
        self.assertEqual(failed["type"], "form")
        self.assertEqual(failed["step_id"], "authorize")
        self.assertEqual(failed["errors"], {"base": "qr_auth_failed"})

        done = await repair.async_step_authorize({"qr_auth_url": "https://example.test/?qrCode=new"})
        self.assertEqual(done["type"], "create_entry")
        self.assertEqual(
            entry.runtime_data.calls,
            ["https://example.test/?qrCode=old", "https://example.test/?qrCode=new"],
        )


class _Client:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.authorize_calls = 0
        self.refresh_calls = 0

    async def authorize_qr_code(self, value: str) -> dict:
        self.authorize_calls += 1
        if self.mode in {"auth-then-ok", "refresh-fails"} and self.authorize_calls == 1:
            raise DeepalAuthError("expired")
        if self.mode == "bad-qr":
            raise DeepalApiError("bad qr")
        if self.mode == "auth-then-bad" and self.authorize_calls == 1:
            raise DeepalAuthError("expired")
        if self.mode == "auth-then-bad":
            raise DeepalApiError("bad qr")
        return {"qr_code": value, "scan": {"vehicleId": "veh"}, "confirm": "suc"}

    async def refresh_tokens(self) -> DeepalTokens:
        self.refresh_calls += 1
        if self.mode == "refresh-fails":
            raise DeepalAuthError("refresh failed")
        return DeepalTokens("new-access", "new-refresh")


class CoordinatorIssueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self._tmp.name)
        frame.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await ir.async_load(self.hass)

    async def asyncTearDown(self) -> None:
        await self.hass.async_stop()
        self._tmp.cleanup()

    def _coordinator(self, mode: str = "ok") -> tuple[DeepalDataUpdateCoordinator, ConfigEntry, _Client]:
        entry = _loaded_entry(self.hass, "veh-entry")
        client = _Client(mode)
        coordinator = DeepalDataUpdateCoordinator(self.hass, entry, client, "veh-1")
        return coordinator, entry, client

    async def test_location_switch_opens_and_closes_repair_issue(self) -> None:
        coordinator, entry, _client = self._coordinator()
        issue_id = f"{entry.entry_id}_location_permission"

        async def fetch_off():
            return [{"carId": "veh-1"}], {"locationGeo": {"locationSwitch": 0, "lat": "48.85", "lng": "0.1", "gpsTime": "2026-01-01 12:00:00"}}

        coordinator._async_fetch = fetch_off
        await coordinator._async_update_data()
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id)
        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertTrue(issue.is_fixable)
        self.assertTrue(issue.is_persistent)
        self.assertEqual(issue.severity, ir.IssueSeverity.WARNING)
        self.assertEqual(issue.translation_key, "location_permission")

        async def fetch_missing():
            return [{"carId": "veh-1"}], {"vehicleStatus": {}}

        coordinator._async_fetch = fetch_missing
        await coordinator._async_update_data()
        self.assertIsNotNone(ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id))

        async def fetch_on():
            return [{"carId": "veh-1"}], {"locationGeo": {"locationSwitch": "1", "lat": "48.86", "lng": "0.0", "gpsTime": "2026-01-01 12:00:00"}}

        coordinator._async_fetch = fetch_on
        await coordinator._async_update_data()
        self.assertIsNone(ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id))

    async def test_authorize_refreshes_tokens_once_and_clears_issue(self) -> None:
        coordinator, entry, client = self._coordinator("auth-then-ok")
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{entry.entry_id}_location_permission",
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="location_permission",
        )
        refreshed: list[bool] = []

        async def refresh() -> None:
            refreshed.append(True)

        coordinator.async_request_refresh = refresh
        result = await coordinator.async_authorize_qr_code("qr-token")
        self.assertEqual(result["confirm"], "suc")
        self.assertEqual(client.authorize_calls, 2)
        self.assertEqual(client.refresh_calls, 1)
        self.assertEqual(entry.data["access_token"], "new-access")
        self.assertEqual(entry.data["refresh_token"], "new-refresh")
        self.assertEqual(refreshed, [True])
        self.assertIsNone(
            ir.async_get(self.hass).async_get_issue(DOMAIN, f"{entry.entry_id}_location_permission")
        )

    async def test_bad_qr_does_not_refresh_tokens(self) -> None:
        coordinator, entry, client = self._coordinator("bad-qr")
        coordinator.async_request_refresh = self._unexpected_refresh
        with self.assertRaises(DeepalApiError):
            await coordinator.async_authorize_qr_code("qr-token")
        self.assertEqual(client.refresh_calls, 0)
        self.assertEqual(entry.data["access_token"], "old-access")

    async def test_refresh_failure_raises_auth_failed(self) -> None:
        coordinator, _entry, client = self._coordinator("refresh-fails")
        coordinator.async_request_refresh = self._unexpected_refresh
        with self.assertRaises(ConfigEntryAuthFailed):
            await coordinator.async_authorize_qr_code("qr-token")
        self.assertEqual(client.refresh_calls, 1)
        self.assertEqual(client.authorize_calls, 1)

    async def _unexpected_refresh(self) -> None:
        raise AssertionError("refresh should not run")


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self._tmp.name)
        self.hass.config_entries = ConfigEntries(self.hass, {})

    async def asyncTearDown(self) -> None:
        await self.hass.async_stop()
        self._tmp.cleanup()

    async def test_single_entry_default_and_explicit_entry(self) -> None:
        first = _loaded_entry(self.hass, "one")
        first.runtime_data = _QrCoordinator()
        _async_register_authorize_qr_service(self.hass)
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_AUTHORIZE_QR,
            {"qr_code": "plain-token"},
            blocking=True,
        )
        self.assertEqual(first.runtime_data.calls, ["plain-token"])

        second = _loaded_entry(self.hass, "two", unique_id="unique-two")
        second.runtime_data = _QrCoordinator()
        with self.assertRaises(HomeAssistantError):
            await async_handle_authorize_qr(
                self.hass,
                ServiceCall(self.hass, DOMAIN, SERVICE_AUTHORIZE_QR, {"qr_code": "again"}),
            )
        await async_handle_authorize_qr(
            self.hass,
            ServiceCall(
                self.hass,
                DOMAIN,
                SERVICE_AUTHORIZE_QR,
                {"qr_code": "for-two", "entry_id": second.entry_id},
            ),
        )
        self.assertEqual(second.runtime_data.calls, ["for-two"])
        self.assertEqual(first.runtime_data.calls, ["plain-token"])

    async def test_api_error_and_unknown_entry(self) -> None:
        entry = _loaded_entry(self.hass, "only")
        entry.runtime_data = _QrCoordinator(fail_times=1)
        with self.assertRaises(HomeAssistantError) as raised:
            await async_handle_authorize_qr(
                self.hass,
                ServiceCall(self.hass, DOMAIN, SERVICE_AUTHORIZE_QR, {"qr_code": "bad"}),
            )
        self.assertIn("failed", str(raised.exception).lower())
        with self.assertRaises(HomeAssistantError):
            await async_handle_authorize_qr(
                self.hass,
                ServiceCall(
                    self.hass,
                    DOMAIN,
                    SERVICE_AUTHORIZE_QR,
                    {"qr_code": "bad", "entry_id": "missing"},
                ),
            )

    async def test_service_unregisters_when_last_entry_unloads(self) -> None:
        first = _loaded_entry(self.hass, "keep")
        second = _loaded_entry(self.hass, "drop", unique_id="unique-drop")
        _async_register_authorize_qr_service(self.hass)
        self.assertTrue(self.hass.services.has_service(DOMAIN, SERVICE_AUTHORIZE_QR))
        second._async_set_state(self.hass, ConfigEntryState.UNLOAD_IN_PROGRESS, None)
        self.assertTrue(await async_unload_entry(self.hass, second))
        second._async_set_state(self.hass, ConfigEntryState.NOT_LOADED, None)
        self.assertTrue(self.hass.services.has_service(DOMAIN, SERVICE_AUTHORIZE_QR))
        first._async_set_state(self.hass, ConfigEntryState.UNLOAD_IN_PROGRESS, None)
        self.assertTrue(await async_unload_entry(self.hass, first))
        self.assertFalse(self.hass.services.has_service(DOMAIN, SERVICE_AUTHORIZE_QR))

    async def test_schema_requires_qr_code(self) -> None:
        _async_register_authorize_qr_service(self.hass)
        with self.assertRaises(vol.Invalid):
            await self.hass.services.async_call(DOMAIN, SERVICE_AUTHORIZE_QR, {}, blocking=True)


class ManifestTests(unittest.TestCase):
    def test_strings_match_and_services_yaml_parses(self) -> None:
        strings = json.loads((_COMPONENT / "strings.json").read_text())
        english = json.loads((_COMPONENT / "translations" / "en.json").read_text())
        self.assertEqual(strings, english)
        self.assertIn("location", strings["entity"]["device_tracker"])
        self.assertEqual(strings["issues"]["location_permission"]["fix_flow"]["error"]["qr_auth_failed"], strings["options"]["error"]["qr_auth_failed"])
        described = _SERVICES_SCHEMA(load_yaml_dict(str(_COMPONENT / "services.yaml")))
        self.assertIn(SERVICE_AUTHORIZE_QR, described)
        self.assertIn("qr_code", described[SERVICE_AUTHORIZE_QR]["fields"])
        self.assertIn("entry_id", described[SERVICE_AUTHORIZE_QR]["fields"])
        self.assertIn(Platform.DEVICE_TRACKER, __import__("custom_components.deepal.const", fromlist=["PLATFORMS"]).PLATFORMS)


def _loaded_entry(hass: HomeAssistant, entry_id: str, unique_id: str | None = None) -> ConfigEntry:
    entry = ConfigEntry(
        data={"access_token": "old-access", "refresh_token": "old-refresh"},
        discovery_keys={},
        domain=DOMAIN,
        entry_id=entry_id,
        minor_version=1,
        options={},
        source="user",
        title="Deepal",
        unique_id=unique_id or entry_id,
        version=1,
        subentries_data=None,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    return entry


if __name__ == "__main__":
    unittest.main()
