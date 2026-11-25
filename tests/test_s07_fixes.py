"""S07 standby charging, charge window service, and login start-over."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import time
from pathlib import Path
from unittest.mock import patch

import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import current_platform

from custom_components.deepal import binary_sensor as binary_sensor_mod
from custom_components.deepal import switch as switch_mod
from custom_components.deepal.api import DeepalClient, _s05_condition_from_params
from custom_components.deepal.binary_sensor import _charge_cable_connected, _dc_charge_gun_connected, _is_charging
from custom_components.deepal.config_flow import DeepalConfigFlow
from custom_components.deepal.const import DOMAIN
from custom_components.deepal.switch import DeepalChargeScheduleSwitch

_COMPONENT = _ROOT / "custom_components" / "deepal"


def _binary(key: str):
    return next(item for item in binary_sensor_mod.BINARY_SENSORS if item.key == key)


def _flow() -> DeepalConfigFlow:
    flow = DeepalConfigFlow()
    flow.flow_id = "flow-1"
    flow.handler = DOMAIN
    flow.context = {"source": "user"}
    return flow


def _schema_names(schema) -> set[str]:
    names: set[str] = set()
    for key in schema.schema:
        names.add(getattr(key, "schema", key))
    return names


class S07ChargeStateTests(unittest.TestCase):
    def test_rest_standby_is_not_charging_and_does_not_force_a_cable(self) -> None:
        standby = {
            "charge": {
                "chargeStatus": 1,
                "chargeConStatus": 1,
                "dcChargeGunConnectStatus": 1,
                "remainChargeTime": 0,
                "acChargeCurrent": 0,
                "dcChargeCurrent": 0,
            }
        }
        self.assertIs(False, _is_charging(standby))
        self.assertIs(False, _binary("charging").value_fn(standby))
        self.assertIs(False, _charge_cable_connected(standby))
        self.assertIsNone(_dc_charge_gun_connected(standby))
        self.assertIsNone(_binary("dc_charge_gun_connected").value_fn(standby))

        unplugged = {"charge": {"chargeStatus": 1, "chargeConStatus": 0, "dcChargeGunConnectStatus": 1}}
        self.assertIs(False, _is_charging(unplugged))
        self.assertIs(False, _charge_cable_connected(unplugged))

    def test_rest_status_6_or_live_remain_time_is_charging(self) -> None:
        active = {
            "charge": {
                "chargeStatus": 6,
                "chargeConStatus": 1,
                "dcChargeGunConnectStatus": 1,
                "remainChargeTime": 8191,
            }
        }
        self.assertIs(True, _is_charging(active))
        self.assertIs(True, _charge_cable_connected(active))
        self.assertIsNone(_dc_charge_gun_connected(active))

        by_time = {"charge": {"chargeStatus": 1, "remainChargeTime": "40", "chargeConStatus": 0}}
        self.assertIs(True, _is_charging(by_time))
        self.assertIs(True, _charge_cable_connected(by_time))
        self.assertIs(True, _is_charging({"charge": {"remainChargeTime": 7999}}))
        self.assertIs(False, _is_charging({"charge": {"chargeStatus": 0, "remainChargeTime": 0}}))
        self.assertIs(False, _is_charging({"charge": {"chargeStatus": 1, "remainChargeTime": 8000}}))
        self.assertIs(False, _is_charging({"charge": {"chargeStatus": 1, "remainChargeTime": 8191}}))

    def test_rest_dc_gun_is_connected_only_when_zero(self) -> None:
        self.assertIs(True, _dc_charge_gun_connected({"charge": {"dcChargeGunConnectStatus": 0}}))
        self.assertIsNone(_dc_charge_gun_connected({"charge": {"dcChargeGunConnectStatus": 1}}))
        self.assertIsNone(_dc_charge_gun_connected({"charge": {}}))
        self.assertIsNone(_is_charging({}))
        self.assertIsNone(_charge_cable_connected({}))
        self.assertIs(
            False,
            _is_charging({"charge": {"remainChargeTime": float("nan"), "acChargeCurrent": 0}}),
        )

    def test_s05_keeps_normalized_charge_and_dc_gun_polarity(self) -> None:
        charging = _s05_condition_from_params({"ChrgSts": 4, "BattDCChrgInCurr": 80})
        self.assertIn("rawS05", charging)
        self.assertEqual(charging["charge"]["chargeStatus"], 4)
        self.assertIs(True, _is_charging(charging))
        self.assertIs(True, _binary("charging").value_fn(charging))
        self.assertIs(True, _charge_cable_connected(charging))
        self.assertIs(True, _dc_charge_gun_connected(charging))

        idle = _s05_condition_from_params({"ChrgSts": 0, "acChargeGunConnectionState": 1, "dcChargeGunConnectionState": 0})
        self.assertIs(False, _is_charging(idle))
        self.assertIs(False, _charge_cable_connected(idle))
        self.assertIs(False, _dc_charge_gun_connected(idle))

        # A non-zero normalized S05 status is charging even when the raw code is 1.
        synthetic = {"rawS05": {"ChrgSts": 3}, "charge": {"chargeStatus": 1, "dcChargeGunConnectStatus": 1}}
        self.assertIs(True, _is_charging(synthetic))
        self.assertIs(False, _dc_charge_gun_connected(synthetic))
        self.assertIsNone(_dc_charge_gun_connected({"rawS05": {}, "charge": {}}))


class _SignedCapture:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def __call__(self, **kwargs):
        self.kwargs = kwargs
        return "cmd-1"


def _client() -> DeepalClient:
    return DeepalClient(
        None,  # type: ignore[arg-type]
        access_token="access",
        refresh_token=None,
        country="GB",
        language="en_US",
        app_version="V1.12.0",
        device_id="device-1",
    )


class ChargeScheduleApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_sets_both_switches(self) -> None:
        client = _client()
        capture = _SignedCapture()
        client._signed_command = capture  # type: ignore[method-assign]
        command_id = await client.control_charge_schedule(
            vehicle_id="car-1",
            plan_id="plan-1",
            start_time="0030",
            end_time="0700",
            enabled=True,
        )
        self.assertEqual(command_id, "cmd-1")
        assert capture.kwargs is not None
        self.assertEqual(capture.kwargs["path"], "/intl-app-gw/intl-app-car-control/api/charge/modify-plan")
        self.assertEqual(
            capture.kwargs["payload"],
            {
                "command": "modify-plan",
                "endSwitch": 1,
                "endTime": "0700",
                "planId": "plan-1",
                "planType": 1,
                "startSwitch": 1,
                "startTime": "0030",
                "timeFormat": 1,
                "timeZone": "GMT+08:00",
            },
        )

        await client.control_charge_schedule(
            vehicle_id="car-1",
            plan_id="plan-1",
            start_time="0000",
            end_time="0000",
            enabled=False,
        )
        assert capture.kwargs is not None
        self.assertEqual(capture.kwargs["payload"]["startSwitch"], 0)
        self.assertEqual(capture.kwargs["payload"]["endSwitch"], 0)

    async def test_start_and_end_switches_can_differ(self) -> None:
        client = _client()
        capture = _SignedCapture()
        client._signed_command = capture  # type: ignore[method-assign]
        await client.control_charge_schedule(
            vehicle_id="car-1",
            plan_id="plan-9",
            start_time="2200",
            end_time="0600",
            enabled=True,
            start_enabled=False,
            end_enabled=True,
            plan_type=2,
            time_zone="GMT+00:00",
        )
        assert capture.kwargs is not None
        payload = capture.kwargs["payload"]
        self.assertEqual(payload["startSwitch"], 0)
        self.assertEqual(payload["endSwitch"], 1)
        self.assertEqual(payload["planId"], "plan-9")
        self.assertEqual(payload["planType"], 2)
        self.assertEqual(payload["timeZone"], "GMT+00:00")


class _Coordinator:
    def __init__(self, *, mqtt: bool, condition: dict | None = None) -> None:
        self.vehicle_id = "car-1"
        self.vehicle_uses_mqtt = mqtt
        self.data = {"condition": condition or {}, "vehicle": {"carName": "Deepal"}}
        self.client = self
        self.calls: list[dict] = []

    async def control_charge_schedule(self, **kwargs):
        self.calls.append(kwargs)
        return "cmd-1"

    async def async_execute_command(self, send_command, **_kwargs) -> None:
        await send_command()


class _Entry:
    def __init__(self, coordinator: _Coordinator) -> None:
        self.runtime_data = coordinator


class _Platform:
    def __init__(self) -> None:
        self.registered: list[tuple] = []

    def async_register_entity_service(self, name, schema, func, *args, **kwargs) -> None:
        self.registered.append((name, schema, func))


class SetChargeWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_window_is_one_command_and_keeps_each_switch(self) -> None:
        coordinator = _Coordinator(
            mqtt=False,
            condition={
                "charge": {
                    "chargePlanList": [
                        {
                            "planId": "plan-7",
                            "startTime": "0000",
                            "endTime": "0000",
                            "startSwitch": 1,
                            "endSwitch": 0,
                            "planType": 3,
                            "timeFormat": 1,
                            "timeZone": "GMT+01:00",
                        }
                    ]
                }
            },
        )
        entity = DeepalChargeScheduleSwitch(coordinator)
        await entity.async_set_charge_window(time(7, 5), time(22, 30))
        self.assertEqual(
            coordinator.calls,
            [
                {
                    "vehicle_id": "car-1",
                    "plan_id": "plan-7",
                    "start_time": "0705",
                    "end_time": "2230",
                    "start_enabled": True,
                    "end_enabled": False,
                    "plan_type": 3,
                    "time_format": 1,
                    "time_zone": "GMT+01:00",
                }
            ],
        )

    async def test_missing_plan_raises(self) -> None:
        entity = DeepalChargeScheduleSwitch(_Coordinator(mqtt=False))
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_charge_window(time(0, 30), time(7, 0))

    async def test_service_registers_only_for_rest_and_survives_no_platform(self) -> None:
        platform = _Platform()
        token = current_platform.set(platform)
        try:
            rest_added: list = []
            await switch_mod.async_setup_entry(None, _Entry(_Coordinator(mqtt=False)), rest_added.extend)
            mqtt_added: list = []
            await switch_mod.async_setup_entry(None, _Entry(_Coordinator(mqtt=True)), mqtt_added.extend)
        finally:
            current_platform.reset(token)

        self.assertEqual([type(entity).__name__ for entity in rest_added], ["DeepalSteeringWheelHeatSwitch", "DeepalChargeScheduleSwitch"])
        self.assertEqual([type(entity).__name__ for entity in mqtt_added], ["DeepalSteeringWheelHeatSwitch"])
        self.assertEqual([item[0] for item in platform.registered], ["set_charge_window"])
        self.assertEqual(platform.registered[0][2], "async_set_charge_window")
        self.assertEqual(
            {getattr(key, "schema", key) for key in platform.registered[0][1]},
            {"start_time", "end_time"},
        )

        added: list = []
        await switch_mod.async_setup_entry(None, _Entry(_Coordinator(mqtt=False)), added.extend)
        self.assertEqual(len(added), 2)


class LoginStartOverTests(unittest.IsolatedAsyncioTestCase):
    async def test_reauth_drops_a_half_finished_login(self) -> None:
        flow = _flow()

        class _Boom:
            def __getattr__(self, name):
                raise AssertionError(name)

        flow.hass = _Boom()  # type: ignore[assignment]
        flow._email_login = {"email": "a@b.c"}
        flow._phone_login = {"mobile": "7000000000"}
        flow._login_result = {"login": {}}
        result = await flow.async_step_reauth({})
        self.assertEqual(result["step_id"], "user")
        self.assertFalse(hasattr(flow, "_email_login"))
        self.assertFalse(hasattr(flow, "_phone_login"))
        self.assertFalse(hasattr(flow, "_login_result"))

    async def test_email_code_start_over_and_blank_code(self) -> None:
        flow = _flow()
        missing = await flow.async_step_email_code(None)
        self.assertEqual(missing["step_id"], "email")

        flow._email_login = {"email": "a@b.c"}
        shown = await flow.async_step_email_code(None)
        self.assertEqual(shown["step_id"], "email_code")
        self.assertEqual(_schema_names(shown["data_schema"]), {"auth_code", "start_over"})
        self.assertEqual(shown["data_schema"]({}), {"auth_code": "", "start_over": False})

        with patch("custom_components.deepal.config_flow.DeepalClient", side_effect=AssertionError("client")):
            blank = await flow.async_step_email_code({"auth_code": "  ", "start_over": False})
            self.assertEqual(blank["errors"]["base"], "bad_email_code")
            self.assertEqual(blank["step_id"], "email_code")
            restarted = await flow.async_step_email_code({"auth_code": "", "start_over": True})
        self.assertEqual(restarted["step_id"], "email")
        self.assertFalse(hasattr(flow, "_email_login"))

    async def test_sms_start_over_and_blank_code(self) -> None:
        flow = _flow()
        missing = await flow.async_step_sms(None)
        self.assertEqual(missing["step_id"], "phone")

        flow._phone_login = {"mobile": "7000000000"}
        shown = await flow.async_step_sms(None)
        self.assertEqual(shown["step_id"], "sms")
        self.assertEqual(_schema_names(shown["data_schema"]), {"auth_code", "start_over"})

        with patch("custom_components.deepal.config_flow.DeepalClient", side_effect=AssertionError("client")):
            blank = await flow.async_step_sms({"start_over": False})
            self.assertEqual(blank["errors"]["base"], "bad_sms_code")
            restarted = await flow.async_step_sms({"auth_code": "123456", "start_over": True})
        self.assertEqual(restarted["step_id"], "phone")
        self.assertFalse(hasattr(flow, "_phone_login"))


class ReleaseMetadataTests(unittest.TestCase):
    def test_strings_match_and_charge_window_is_documented(self) -> None:
        strings = json.loads((_COMPONENT / "strings.json").read_text())
        english = json.loads((_COMPONENT / "translations" / "en.json").read_text())
        self.assertEqual(strings, english)
        for step in ("email_code", "sms"):
            data = strings["config"]["step"][step]
            self.assertEqual(data["data"]["start_over"], "Start over")
            self.assertIn("start_over", data["data_description"])
        services = yaml.safe_load((_COMPONENT / "services.yaml").read_text())
        self.assertIn("authorize_qr", services)
        self.assertIn("set_charge_window", services)
        self.assertEqual(json.loads((_COMPONENT / "manifest.json").read_text())["version"], "0.4.0")


if __name__ == "__main__":
    unittest.main()
