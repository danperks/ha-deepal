"""S05 MQTT climate and comfort commands, and S07 REST routing."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = str(Path(__file__).resolve().parents[1])
if sys.path[0] != _ROOT:
    sys.path.insert(0, _ROOT)

from homeassistant.components.climate import HVACMode

from custom_components.deepal.api import (
    DeepalApiError,
    DeepalAuthError,
    DeepalClient,
    DeepalCommandNotReady,
    _mqtt_parse_publish,
    _mqtt_publish_packet,
    _s05_aes_decrypt,
)
from custom_components.deepal.button import DeepalFlashLightsButton, DeepalHonkHornButton
from custom_components.deepal.climate import DeepalClimate
from custom_components.deepal.number import DeepalSeatLevelNumber
from custom_components.deepal.switch import DeepalSteeringWheelHeatSwitch

_SECRET = "0123456789abcdef"
_LOGIN_REQ = "$vdp/login-did/loginout/req"
_COMMAND_REQ = "$vdp/device-did/commands/req"
_COMMAND_RES = "$vdp/device-did/commands/res"
_MQTT_CONFIG = {
    "mqttConnectionInfos": [
        {
            "clusterInfos": [{"brokerUrl": "ssl://broker.test", "brokerPort": 8883}],
            "topicInfos": [
                {
                    "msgType": "loginout",
                    "pubTopics": [_LOGIN_REQ],
                    "subTopics": ["$vdp/login-did/loginout/res"],
                },
                {
                    "msgType": "commands",
                    "pubTopics": [_COMMAND_REQ],
                    "subTopics": [_COMMAND_RES],
                },
            ],
        }
    ]
}
_CONNACK = bytes([0x20, 0x02, 0x00, 0x00])
_SUBACK = bytes([0x90, 0x03, 0x00, 0x01, 0x00])
_PEM: str | None = None


def _private_key() -> str:
    global _PEM
    if _PEM is None:
        _PEM = DeepalClient.generate_login_keypair()[0]
    return _PEM


def _split_packets(data: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    packets: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(data):
        if pos + 2 > len(data):
            break
        first = data[pos]
        index = pos + 1
        multiplier = 1
        remaining = 0
        complete = False
        while index < len(data):
            byte = data[index]
            index += 1
            remaining += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                complete = True
                break
            multiplier *= 128
        if not complete or index + remaining > len(data):
            break
        packets.append((first, data[index : index + remaining]))
        pos = index + remaining
    return packets, data[pos:]


class _Writer:
    def __init__(self, on_packets) -> None:
        self._buf = bytearray()
        self._on_packets = on_packets

    def write(self, data: bytes) -> None:
        self._buf.extend(data)

    async def drain(self) -> None:
        packets, rest = _split_packets(bytes(self._buf))
        self._buf = bytearray(rest)
        if packets:
            await self._on_packets(packets)

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _Broker:
    """In-memory MQTT broker that answers the S05 wake-then-command exchange."""

    def __init__(self) -> None:
        self.reader: asyncio.StreamReader | None = None
        self.commands: list[dict] = []
        self.topics: list[str] = []
        self.connects = 0

    def reset(self) -> None:
        self.commands = []
        self.topics = []
        self.connects = 0

    async def open_connection(self, host, port, *, ssl=None, server_hostname=None):
        self.connects += 1
        self.reader = asyncio.StreamReader()
        return self.reader, _Writer(self._on_packets)

    async def _on_packets(self, packets: list[tuple[int, bytes]]) -> None:
        assert self.reader is not None
        for first, body in packets:
            kind = first >> 4
            if kind == 1:
                self.reader.feed_data(_CONNACK)
            elif kind == 8:
                self.reader.feed_data(_SUBACK)
            elif kind == 3:
                topic, payload, _packet_id = _mqtt_parse_publish(first, body)
                self.topics.append(topic)
                if payload.get("mt") == "loginout":
                    self.reader.feed_data(
                        _mqtt_publish_packet(
                            "$vdp/login-did/loginout/res",
                            {"rs": [{"params": {"secretKey": _SECRET}}]},
                        )
                    )
                    continue
                if payload.get("mt") != "commands":
                    continue
                req_id = payload["r"]
                items = _s05_aes_decrypt(payload["sers"], _SECRET, req_id)
                self.commands.append(items[0])
                self.reader.feed_data(_mqtt_publish_packet(_COMMAND_RES, {"r": req_id, "code": 0}))


def _client(**overrides) -> DeepalClient:
    values = {
        "access_token": "access",
        "refresh_token": "refresh",
        "country": "IT",
        "language": "en_US",
        "app_version": "V1.12.0",
        "device_id": "device-1",
        "private_key_pem": _private_key(),
        "enable_commands": True,
        "control_pin": "1234",
        "user_id": "user-1",
    }
    values.update(overrides)
    return DeepalClient(None, **values)  # type: ignore[arg-type]


def _decrypt_call(call: dict) -> list[dict]:
    envelope = DeepalClient._s05_command_request_payload(
        "device-did",
        "login-did",
        _SECRET,
        "device-did_1",
        service_code=call["service_code"],
        method=call["method"],
        params=call["params"],
    )
    return _s05_aes_decrypt(envelope["sers"], _SECRET, "device-did_1")


class _Capture:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, vehicle_id: str, *, service_code: str, method: str, params: dict) -> str:
        self.calls.append(
            {
                "vehicle_id": vehicle_id,
                "service_code": service_code,
                "method": method,
                "params": params,
            }
        )
        return "captured"


class S05CommandPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def _recorded(self, send) -> dict:
        client = _client()
        capture = _Capture()
        client._s05_mqtt_command = capture  # type: ignore[method-assign]
        await send(client)
        self.assertEqual(len(capture.calls), 1)
        call = capture.calls[0]
        self.assertEqual(call["vehicle_id"], "car-1")
        decrypted = _decrypt_call(call)
        self.assertEqual(
            decrypted,
            [
                {
                    "service_code": call["service_code"],
                    "command_code": call["method"],
                    "params": call["params"],
                }
            ],
        )
        return call

    async def test_climate_on_sends_driver_setpoint_in_degrees(self) -> None:
        call = await self._recorded(
            lambda client: client.s05_control_air_conditioner(
                vehicle_id="car-1", enabled=True, target_temp_c=21.5
            )
        )
        self.assertEqual(call["service_code"], "AC")
        self.assertEqual(call["method"], "Cnr_DrACTemp")
        self.assertEqual(call["params"], {"ACTempSet": 21.5, "SetTime": 15})
        self.assertNotIn("ACReq", call["params"])
        self.assertNotEqual(call["params"]["ACTempSet"], 215)

    async def test_climate_off_sends_empty_all_ac_off(self) -> None:
        call = await self._recorded(
            lambda client: client.s05_control_air_conditioner(
                vehicle_id="car-1", enabled=False, target_temp_c=21.5
            )
        )
        self.assertEqual(call["service_code"], "AC")
        self.assertEqual(call["method"], "Cnr_AllAcOff")
        self.assertEqual(call["params"], {})

    async def test_seat_heat_and_vent_include_off_level(self) -> None:
        expected = [
            (lambda client: client.s05_control_seats_heat(vehicle_id="car-1", driver=True, level=3), "Cnr_DrSeatHeat", "SeatHeat", 3),
            (lambda client: client.s05_control_seats_heat(vehicle_id="car-1", driver=False, level=0), "Cnr_PsgSeatHeat", "SeatHeat", 0),
            (lambda client: client.s05_control_seats_wind(vehicle_id="car-1", driver=True, level=2), "Cnr_DrSeatVent", "SeatVent", 2),
            (lambda client: client.s05_control_seats_wind(vehicle_id="car-1", driver=False, level=0), "Cnr_PsgSeatVent", "SeatVent", 0),
        ]
        for send, method, key, level in expected:
            with self.subTest(method=method, level=level):
                call = await self._recorded(send)
                self.assertEqual(call["service_code"], "Seat")
                self.assertEqual(call["method"], method)
                self.assertEqual(call["params"], {key: level, "SetTime": 15})

    async def test_steering_uses_telemetry_enum_without_runtime(self) -> None:
        on = await self._recorded(
            lambda client: client.s05_control_steering_wheel_heat(vehicle_id="car-1", open_value=True)
        )
        off = await self._recorded(
            lambda client: client.s05_control_steering_wheel_heat(vehicle_id="car-1", open_value=False)
        )
        self.assertEqual(on["service_code"], "TxSteering")
        self.assertEqual(on["method"], "Cnr_TxSteeringHeat")
        self.assertEqual(on["params"], {"SteeringHeat": 2})
        self.assertEqual(off["params"], {"SteeringHeat": 1})
        self.assertNotIn("SetTime", on["params"])
        self.assertNotIn("SetTime", off["params"])


class S05CommandGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_unavailable_does_not_connect_or_check_pin(self) -> None:
        client = _client(enable_commands=False, private_key_pem=None, control_pin="1234")

        async def fail_open(*_args, **_kwargs):
            raise AssertionError("open_connection should not be called")

        async def fail_pin(_pin):
            raise AssertionError("check_control_code should not be called")

        client.check_control_code = fail_pin  # type: ignore[method-assign]
        with patch("custom_components.deepal.api.asyncio.open_connection", fail_open):
            with self.assertRaises(DeepalCommandNotReady):
                await client.s05_control_air_conditioner(vehicle_id="car-1", enabled=True, target_temp_c=21.5)

    async def test_pin_is_checked_once_before_a_single_publish(self) -> None:
        client = _client()
        broker = _Broker()
        order: list[tuple] = []

        async def check(pin: str) -> str:
            order.append(("pin", pin, broker.connects, list(broker.commands)))
            return "rc-token"

        client.check_control_code = check  # type: ignore[method-assign]
        client._s05_mqtt_config = lambda _vehicle_id: _async_value(_MQTT_CONFIG)  # type: ignore[method-assign]
        client._s05_mqtt_token = lambda: _async_value("token")  # type: ignore[method-assign]

        cases = [
            (
                lambda: client.s05_control_air_conditioner(vehicle_id="car-1", enabled=True, target_temp_c=21.5),
                {"service_code": "AC", "command_code": "Cnr_DrACTemp", "params": {"ACTempSet": 21.5, "SetTime": 15}},
            ),
            (
                lambda: client.s05_control_air_conditioner(vehicle_id="car-1", enabled=False, target_temp_c=21.5),
                {"service_code": "AC", "command_code": "Cnr_AllAcOff", "params": {}},
            ),
            (
                lambda: client.s05_control_seats_heat(vehicle_id="car-1", driver=False, level=0),
                {"service_code": "Seat", "command_code": "Cnr_PsgSeatHeat", "params": {"SeatHeat": 0, "SetTime": 15}},
            ),
            (
                lambda: client.s05_control_steering_wheel_heat(vehicle_id="car-1", open_value=True),
                {"service_code": "TxSteering", "command_code": "Cnr_TxSteeringHeat", "params": {"SteeringHeat": 2}},
            ),
        ]
        with (
            patch("custom_components.deepal.api.asyncio.open_connection", broker.open_connection),
            patch("custom_components.deepal.api._S05_WAKE_SETTLE_SECONDS", 0),
            patch("custom_components.deepal.api._S05_COMMAND_TIMEOUT", 5),
        ):
            for send, expected in cases:
                with self.subTest(command=expected["command_code"]):
                    broker.reset()
                    order.clear()
                    req_id = await send()
                    self.assertTrue(req_id)
                    self.assertEqual(order, [("pin", "1234", 0, [])])
                    self.assertEqual(broker.connects, 1)
                    self.assertEqual(
                        broker.commands,
                        [
                            {"service_code": "TxWakeup", "command_code": "Cnr_ReWakeup", "params": {}},
                            expected,
                        ],
                    )
                    self.assertEqual(broker.topics.count(_COMMAND_REQ), 2)

    async def test_rejected_pin_does_not_open_a_connection(self) -> None:
        client = _client()
        broker = _Broker()

        async def check(_pin: str) -> str:
            raise DeepalAuthError("bad pin")

        client.check_control_code = check  # type: ignore[method-assign]
        with patch("custom_components.deepal.api.asyncio.open_connection", broker.open_connection):
            with self.assertRaises(DeepalAuthError):
                await client.s05_control_seats_wind(vehicle_id="car-1", driver=True, level=1)
        self.assertEqual(broker.connects, 0)


def _async_value(value):
    async def inner(*_args, **_kwargs):
        return value

    return inner()


class _Coordinator:
    def __init__(self, *, mqtt: bool, condition: dict | None = None) -> None:
        self.hass = None
        self.vehicle_id = "car-1"
        self.client = type("Client", (), {"calls": []})()
        self.data = {
            "vehicle": {"protocolType": "MQTT" if mqtt else "HTTPS", "carId": "car-1"},
            "condition": condition or {},
        }
        self.last_update_success = True

    @property
    def vehicle_uses_mqtt(self) -> bool:
        return str((self.data.get("vehicle") or {}).get("protocolType") or "").upper() == "MQTT"

    async def async_execute_command(self, send_command, **_kwargs) -> None:
        await send_command()

    def bind(self, *names: str) -> None:
        for name in names:

            async def fn(*_args, _name=name, **kwargs):
                self.client.calls.append((_name, kwargs))
                return "cmd-id"

            setattr(self.client, name, fn)


class S05EntityRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_climate_routes_mqtt_and_rest(self) -> None:
        for mqtt, method in ((True, "s05_control_air_conditioner"), (False, "control_air_conditioner")):
            with self.subTest(mqtt=mqtt):
                coordinator = _Coordinator(
                    mqtt=mqtt,
                    condition={"hvac": {"remoteTemp": 215, "acStatus": 0, "insideTemp": 180}},
                )
                coordinator.bind("s05_control_air_conditioner", "control_air_conditioner")
                entity = DeepalClimate(coordinator)
                await entity.async_set_temperature(temperature=22)
                await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
                await entity.async_set_hvac_mode(HVACMode.OFF)
                self.assertEqual(
                    coordinator.client.calls,
                    [
                        (method, {"vehicle_id": "car-1", "enabled": True, "target_temp_c": 22.0}),
                        (method, {"vehicle_id": "car-1", "enabled": True, "target_temp_c": 21.5}),
                        (method, {"vehicle_id": "car-1", "enabled": False, "target_temp_c": 21.5}),
                    ],
                )

    async def test_seats_route_mqtt_and_rest(self) -> None:
        cases = [
            (True, True, "heat", 3, "s05_control_seats_heat"),
            (True, False, "heat", 0, "s05_control_seats_heat"),
            (True, True, "vent", 2, "s05_control_seats_wind"),
            (True, False, "vent", 0, "s05_control_seats_wind"),
            (False, True, "heat", 1, "control_seats_heat"),
            (False, False, "vent", 0, "control_seats_wind"),
        ]
        for mqtt, driver, function, level, method in cases:
            with self.subTest(mqtt=mqtt, driver=driver, function=function, level=level):
                coordinator = _Coordinator(mqtt=mqtt)
                coordinator.bind(
                    "s05_control_seats_heat",
                    "s05_control_seats_wind",
                    "control_seats_heat",
                    "control_seats_wind",
                )
                entity = DeepalSeatLevelNumber(coordinator, driver=driver, function=function)
                await entity.async_set_native_value(level)
                self.assertEqual(
                    coordinator.client.calls,
                    [(method, {"vehicle_id": "car-1", "driver": driver, "level": level})],
                )

    async def test_steering_routes_and_reads_mqtt_enum(self) -> None:
        for mqtt, method in ((True, "s05_control_steering_wheel_heat"), (False, "control_steering_wheel_heat")):
            with self.subTest(mqtt=mqtt):
                coordinator = _Coordinator(mqtt=mqtt, condition={"vehicleStatus": {"steeringWheelHeater": 1}})
                coordinator.bind("s05_control_steering_wheel_heat", "control_steering_wheel_heat")
                entity = DeepalSteeringWheelHeatSwitch(coordinator)
                await entity.async_turn_on()
                await entity.async_turn_off()
                self.assertEqual(
                    coordinator.client.calls,
                    [
                        (method, {"vehicle_id": "car-1", "open_value": True}),
                        (method, {"vehicle_id": "car-1", "open_value": False}),
                    ],
                )

        expectations = [
            (True, None, None),
            (True, 0, False),
            (True, 1, False),
            (True, 2, True),
            (False, None, None),
            (False, 0, False),
            (False, 1, True),
            (False, True, True),
        ]
        for mqtt, raw, expected in expectations:
            with self.subTest(mqtt=mqtt, raw=raw):
                condition = {} if raw is None else {"vehicleStatus": {"steeringWheelHeater": raw}}
                entity = DeepalSteeringWheelHeatSwitch(_Coordinator(mqtt=mqtt, condition=condition))
                self.assertIs(entity.is_on, expected)

    async def test_flash_and_horn_stay_on_rest_for_mqtt_vehicles(self) -> None:
        for mqtt in (True, False):
            with self.subTest(mqtt=mqtt):
                coordinator = _Coordinator(mqtt=mqtt)
                coordinator.bind("control_flashing_honking")
                await DeepalFlashLightsButton(coordinator).async_press()
                await DeepalHonkHornButton(coordinator).async_press()
                self.assertEqual(
                    coordinator.client.calls,
                    [
                        ("control_flashing_honking", {"vehicle_id": "car-1", "action_type": 1}),
                        ("control_flashing_honking", {"vehicle_id": "car-1", "action_type": 3}),
                    ],
                )


class S05TimeoutDoesNotRepublishTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_command_ack_is_not_sent_again(self) -> None:
        client = _client(control_pin=None, rc_token="rc-token")
        silent = _SilentAfterWake()
        client._s05_mqtt_config = lambda _vehicle_id: _async_value(_MQTT_CONFIG)  # type: ignore[method-assign]
        client._s05_mqtt_token = lambda: _async_value("token")  # type: ignore[method-assign]

        async def fail_pin(_pin: str) -> str:
            raise AssertionError("pin check is not required when only rc_token is set")

        client.check_control_code = fail_pin  # type: ignore[method-assign]
        with (
            patch("custom_components.deepal.api.asyncio.open_connection", silent.open_connection),
            patch("custom_components.deepal.api._S05_WAKE_SETTLE_SECONDS", 0),
            patch("custom_components.deepal.api._S05_COMMAND_TIMEOUT", 0.4),
        ):
            with self.assertRaises(DeepalApiError):
                await client.s05_control_air_conditioner(vehicle_id="car-1", enabled=True, target_temp_c=19)
        physical = [item for item in silent.commands if item["service_code"] != "TxWakeup"]
        wake = [item for item in silent.commands if item["service_code"] == "TxWakeup"]
        self.assertEqual(len(wake), 1)
        self.assertEqual(
            physical,
            [{"service_code": "AC", "command_code": "Cnr_DrACTemp", "params": {"ACTempSet": 19.0, "SetTime": 15}}],
        )


class _SilentAfterWake(_Broker):
    async def _on_packets(self, packets: list[tuple[int, bytes]]) -> None:
        assert self.reader is not None
        for first, body in packets:
            kind = first >> 4
            if kind == 1:
                self.reader.feed_data(_CONNACK)
            elif kind == 8:
                self.reader.feed_data(_SUBACK)
            elif kind == 3:
                topic, payload, _packet_id = _mqtt_parse_publish(first, body)
                self.topics.append(topic)
                if payload.get("mt") == "loginout":
                    self.reader.feed_data(
                        _mqtt_publish_packet(
                            "$vdp/login-did/loginout/res",
                            {"rs": [{"params": {"secretKey": _SECRET}}]},
                        )
                    )
                    continue
                if payload.get("mt") != "commands":
                    continue
                req_id = payload["r"]
                items = _s05_aes_decrypt(payload["sers"], _SECRET, req_id)
                self.commands.append(items[0])
                if items[0]["service_code"] == "TxWakeup":
                    self.reader.feed_data(_mqtt_publish_packet(_COMMAND_RES, {"r": req_id, "code": 0}))
