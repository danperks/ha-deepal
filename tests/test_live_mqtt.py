"""Opt-in S05 live MQTT: merge, keepalive, single client id, backoff, unload."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_ROOT = str(Path(__file__).resolve().parents[1])
if sys.path[0] != _ROOT:
    sys.path.insert(0, _ROOT)

from custom_components.deepal.api import (  # noqa: E402
    DeepalAuthError,
    DeepalClient,
    _mqtt_parse_publish,
    _mqtt_read_packet,
    _mqtt_remaining_length,
    _mqtt_string,
    _s05_aes_decrypt,
    _s05_aes_encrypt,
)
from custom_components.deepal.const import (  # noqa: E402
    CONF_ENABLE_LIVE_MQTT,
    MQTT_LIVE_BACKOFF_SECONDS,
)
from custom_components.deepal.coordinator import should_start_live_mqtt  # noqa: E402
from custom_components.deepal.live_mqtt import S05LiveMqttSession  # noqa: E402

_TOKEN = "auth-token-UNIQUE456"
_LOGIN_DID = "login-did-UNIQUE123"
_DEVICE_DID = "device-did-UNIQUE789"
_LAT = "48.8584"
_LNG = "-0.123456"
_SECRET = "0123456789abcdef"
_FORBIDDEN = (_TOKEN, _LOGIN_DID, _DEVICE_DID, _LAT, _LNG, _SECRET, "$vdp/")
_INITIAL = {
    "soc": 80,
    "driverDoorLock": 1,
    "totalOdometer": 12345,
    "lat": _LAT,
    "lng": _LNG,
    "driverDoor": 0,
    "passengerDoor": 0,
    "trunk": 0,
    "hoodStatus": 0,
    "airStatus": 0,
    "vehicleTemperature": 21.5,
}


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


_PRIVATE_KEY = _pem()


def _encode_publish(topic: str, payload: dict, packet_id: int | None = None) -> bytes:
    body = _mqtt_string(topic)
    first = 0x30
    if packet_id is not None:
        body += struct.pack("!H", packet_id)
        first = 0x32
    body += json.dumps(payload, separators=(",", ":")).encode()
    return bytes([first]) + _mqtt_remaining_length(len(body)) + body


def _config(port: int) -> dict:
    login = f"$vdp/{_LOGIN_DID}/loginout"
    device = f"$vdp/{_DEVICE_DID}"
    return {
        "mqttConnectionInfos": [
            {
                "clusterInfos": [{"brokerUrl": "ssl://127.0.0.1", "brokerPort": port}],
                "topicInfos": [
                    {
                        "msgType": "loginout",
                        "pubTopics": [f"{login}/req"],
                        "subTopics": [f"{login}/res"],
                    },
                    {
                        "msgType": "properties",
                        "pubTopics": [f"{device}/properties/get/req"],
                        "subTopics": [
                            f"{device}/properties/get/res",
                            f"{device}/properties/report",
                        ],
                    },
                    {
                        "msgType": "commands",
                        "pubTopics": [f"{device}/commands/req"],
                        "subTopics": [f"{device}/commands/res"],
                    },
                ],
            }
        ]
    }


class _LogCatch(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))
        if record.exc_info and record.exc_info[1] is not None:
            self.lines.append(str(record.exc_info[1]))


class FakeBroker:
    """In-memory MQTT broker. It speaks just enough of the S05 exchange to test the client."""

    def __init__(self) -> None:
        self.mode = "stream"
        self.accepts = 0
        self.current = 0
        self.peak = 0
        self.pingreqs = 0
        self.pubacks: list[int] = []
        self.port = 0
        self.initial = dict(_INITIAL)
        self._packet_id = 0
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.report_topic = f"$vdp/{_DEVICE_DID}/properties/get/res"
        self.command_res = f"$vdp/{_DEVICE_DID}/commands/res"
        self.login_res = f"$vdp/{_LOGIN_DID}/loginout/res"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sockets = self._server.sockets or []
        self.port = sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def publish(self, topic: str, payload: dict, *, qos1: bool = False) -> None:
        writer = self._writer
        if writer is None:
            raise AssertionError("broker has no client")
        packet_id = None
        if qos1:
            self._packet_id = self._packet_id % 65535 + 1
            packet_id = self._packet_id
        writer.write(_encode_publish(topic, payload, packet_id))
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.accepts += 1
        self.current += 1
        self.peak = max(self.peak, self.current)
        self._writer = writer
        try:
            if self.mode == "drop":
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                return
            await self._speak(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, BrokenPipeError):
            return
        finally:
            self.current -= 1
            if self._writer is writer:
                self._writer = None
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _speak(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _mqtt_read_packet(reader)
        writer.write(bytes([0x20, 0x02, 0x00, 0x00]))
        await writer.drain()
        _first, body = await _mqtt_read_packet(reader)
        packet_id = struct.unpack("!H", body[:2])[0]
        writer.write(bytes([0x90, 0x03]) + struct.pack("!H", packet_id) + bytes([0x00]))
        await writer.drain()
        while True:
            first, packet = await _mqtt_read_packet(reader)
            packet_type = first >> 4
            if packet_type == 12:
                self.pingreqs += 1
                writer.write(bytes([0xD0, 0x00]))
                await writer.drain()
                continue
            if packet_type == 4 and len(packet) >= 2:
                self.pubacks.append(struct.unpack("!H", packet[:2])[0])
                continue
            if packet_type != 3:
                continue
            topic, payload, _qos_id = _mqtt_parse_publish(first, packet)
            if "/loginout/req" in topic:
                await self.publish_on(
                    writer,
                    self.login_res,
                    {"rs": [{"params": {"secretKey": _SECRET}}]},
                )
            elif "/properties/get/req" in topic:
                await self.publish_on(writer, self.report_topic, self._condition_payload(self.initial), qos1=True)
            elif "/commands/req" in topic and isinstance(payload, dict):
                await self._reply_command(writer, payload)

    async def publish_on(
        self,
        writer: asyncio.StreamWriter,
        topic: str,
        payload: dict,
        *,
        qos1: bool = False,
    ) -> None:
        packet_id = None
        if qos1:
            self._packet_id = self._packet_id % 65535 + 1
            packet_id = self._packet_id
        writer.write(_encode_publish(topic, payload, packet_id))
        await writer.drain()

    def _condition_payload(self, params: dict) -> dict:
        req_id = "report-1"
        return {
            "r": req_id,
            "rs": _s05_aes_encrypt(
                [{"service_code": "car_condition", "params": params}],
                _SECRET,
                req_id,
            ),
        }

    async def _reply_command(self, writer: asyncio.StreamWriter, payload: dict) -> None:
        req_id = payload.get("r")
        encrypted = payload.get("sers")
        if not isinstance(req_id, str) or not isinstance(encrypted, str):
            return
        _s05_aes_decrypt(encrypted, _SECRET, req_id)
        await self.publish_on(
            writer,
            self.command_res,
            {
                "r": req_id,
                "rs": _s05_aes_encrypt([{"success": True, "code": "000000"}], _SECRET, req_id),
            },
        )


def _client() -> DeepalClient:
    client = DeepalClient(
        session=None,  # type: ignore[arg-type]
        access_token="access-not-the-mqtt-token",
        refresh_token=None,
        country="GB",
        language="en_US",
        app_version="V1.12.0",
        device_id="phone-device",
        private_key_pem=_PRIVATE_KEY,
        enable_commands=True,
        rc_token="rc-token",
        user_id="user-1",
    )
    return client


class LiveMqttTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.logs = _LogCatch()
        self.logger = logging.getLogger("custom_components.deepal")
        self._level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.logs)
        self.broker = FakeBroker()
        await self.broker.start()
        self.client = _client()

        async def opener(host: str, port: int):
            return await asyncio.open_connection("127.0.0.1", self.broker.port)

        async def config(vehicle_id: str):
            return _config(self.broker.port)

        async def token() -> str:
            return _TOKEN

        self.client._s05_mqtt_config = config  # type: ignore[method-assign]
        self.client._s05_mqtt_token = token  # type: ignore[method-assign]
        self._opener = patch("custom_components.deepal.api._open_mqtt_connection", opener)
        self._opener.start()
        self.updates: list[dict] = []
        self.session: S05LiveMqttSession | None = None
        self.task: asyncio.Task[None] | None = None

    async def asyncTearDown(self) -> None:
        if self.session is not None:
            self.session.request_stop()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self._opener.stop()
        await self.broker.close()
        self.logger.removeHandler(self.logs)
        self.logger.setLevel(self._level)

    def _on_condition(self, condition: dict) -> None:
        self.updates.append(condition)

    async def _wait_broker_idle(self, timeout: float = 1) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.broker.current != 0:
            if loop.time() > deadline:
                self.fail(f"broker still has {self.broker.current} connection(s)")
            await asyncio.sleep(0.01)

    async def _wait_updates(self, count: int, timeout: float = 2) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.updates) < count:
            if loop.time() > deadline:
                self.fail(f"expected {count} live updates, saw {len(self.updates)}")
            await asyncio.sleep(0.02)

    async def _start(self, **kwargs: object) -> S05LiveMqttSession:
        session = S05LiveMqttSession(
            self.client,
            "veh-1",
            self._on_condition,
            coalesce_seconds=float(kwargs.get("coalesce_seconds", 0.2)),
            keepalive_seconds=float(kwargs.get("keepalive_seconds", 30)),
            refresh_auth=kwargs.get("refresh_auth"),  # type: ignore[arg-type]
            sleep=kwargs.get("sleep"),  # type: ignore[arg-type]
            backoff_seconds=kwargs.get("backoff_seconds", MQTT_LIVE_BACKOFF_SECONDS),  # type: ignore[arg-type]
        )
        self.session = session
        self.task = asyncio.create_task(session.run())
        return session

    def _assert_redacted(self) -> None:
        text = "\n".join(self.logs.lines)
        for secret in _FORBIDDEN:
            self.assertNotIn(secret, text)

    async def test_initial_snapshot_and_partial_delta_merge(self) -> None:
        await self._start()
        await self.session.wait_until_healthy(3)
        await self._wait_updates(1)
        first = self.updates[0]
        self.assertEqual(first["vehicleStatus"]["soc"], 80)
        self.assertEqual(first["door"]["driverLock"], 1)
        self.assertEqual(first["rawS05"]["totalOdometer"], 12345)

        before = len(self.updates)
        report = f"$vdp/{_DEVICE_DID}/properties/report"
        await self.broker.publish(report, self.broker._condition_payload({"driverDoorLock": 0}))
        await self.broker.publish(report, self.broker._condition_payload({"hoodStatus": 1}))
        await asyncio.sleep(0.08)
        self.assertEqual(len(self.updates), before)
        await self._wait_updates(before + 1, timeout=1)
        self.assertEqual(len(self.updates), before + 1)
        merged = self.updates[-1]
        self.assertEqual(merged["vehicleStatus"]["soc"], 80)
        self.assertEqual(merged["door"]["driverLock"], 0)
        self.assertEqual(merged["door"]["hood"], 1)
        self.assertEqual(merged["vehicleStatus"]["totalMileage"], 12345.0)
        self.assertEqual(merged["rawS05"]["soc"], 80)
        self.assertEqual(self.session.raw_params["soc"], 80)
        self.assertEqual(self.session.raw_params["driverDoorLock"], 0)
        self._assert_redacted()

    async def test_keepalive_ping_and_puback(self) -> None:
        await self._start(keepalive_seconds=0.25)
        await self.session.wait_until_healthy(3)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2
        while self.broker.pingreqs < 1 or not self.broker.pubacks:
            if loop.time() > deadline:
                break
            await asyncio.sleep(0.05)
        self.assertGreaterEqual(self.broker.pingreqs, 1)
        self.assertTrue(self.broker.pubacks)
        self.assertTrue(self.session.healthy)
        self.assertEqual(self.broker.current, 1)
        self._assert_redacted()

    async def test_poll_does_not_open_a_second_connection(self) -> None:
        await self._start()
        await self.session.wait_until_healthy(3)
        await self._wait_updates(1)
        accepts = self.broker.accepts
        condition = await self.client.s05_mqtt_condition("veh-1")
        self.assertEqual(condition["vehicleStatus"]["soc"], 80)
        self.assertEqual(self.broker.accepts, accepts)
        self.assertEqual(self.broker.peak, 1)
        self._assert_redacted()

    async def test_command_yields_the_client_id(self) -> None:
        await self._start()
        await self.session.wait_until_healthy(3)
        await self._wait_updates(1)
        self.broker.initial["soc"] = 55
        with patch("custom_components.deepal.api._S05_WAKE_SETTLE_SECONDS", 0):
            command_id = await self.client.s05_control_doors(vehicle_id="veh-1", open_value=False)
        self.assertTrue(command_id)
        self.assertEqual(self.broker.peak, 1)
        # The shared client id was released. The pre-command snapshot must not
        # be served again; the resumed session requests a fresh property set.
        cached = self.session.latest_condition()
        if cached is not None:
            self.assertEqual(cached["vehicleStatus"]["soc"], 55)
        await self.session.wait_until_healthy(3)
        condition = await self.client.s05_mqtt_condition("veh-1")
        self.assertEqual(condition["vehicleStatus"]["soc"], 55)
        self.assertEqual(self.broker.peak, 1)
        self.assertEqual(self.broker.current, 1)
        self.assertTrue(self.session.healthy)
        self._assert_redacted()

    async def test_reconnect_backoff_sequence(self) -> None:
        self.broker.mode = "drop"
        delays: list[float] = []

        async def sleeper(delay: float) -> None:
            delays.append(delay)
            if len(delays) >= 4:
                session.request_stop()

        session = S05LiveMqttSession(
            self.client,
            "veh-1",
            self._on_condition,
            sleep=sleeper,
            backoff_seconds=MQTT_LIVE_BACKOFF_SECONDS,
        )
        self.session = session
        await session.run()
        self.assertEqual(delays, [5, 15, 60, 300])
        self.assertGreaterEqual(self.broker.accepts, 4)
        self.assertEqual(self.broker.current, 0)
        self._assert_redacted()

    async def test_auth_error_refreshes_then_connects(self) -> None:
        calls = {"config": 0, "refresh": 0}

        async def config(vehicle_id: str):
            calls["config"] += 1
            if calls["config"] == 1:
                raise DeepalAuthError(f"invalided token {_TOKEN}")
            return _config(self.broker.port)

        async def refresh() -> bool:
            calls["refresh"] += 1
            return True

        self.client._s05_mqtt_config = config  # type: ignore[method-assign]
        await self._start(refresh_auth=refresh)
        await self.session.wait_until_healthy(3)
        await self._wait_updates(1)
        self.assertEqual(calls["refresh"], 1)
        self.assertGreaterEqual(calls["config"], 2)
        self.assertEqual(self.updates[0]["vehicleStatus"]["soc"], 80)
        self._assert_redacted()

    async def test_log_failure_omits_payload_tokens_and_topic_ids(self) -> None:
        async def boom(host: str, port: int):
            raise OSError(f"token={_TOKEN} did={_DEVICE_DID} lat={_LAT} topic=$vdp/{_DEVICE_DID}/properties/report")

        self._opener.stop()
        self._opener = patch("custom_components.deepal.api._open_mqtt_connection", boom)
        self._opener.start()

        async def sleeper(delay: float) -> None:
            session.request_stop()

        session = S05LiveMqttSession(self.client, "veh-1", self._on_condition, sleep=sleeper)
        self.session = session
        await session.run()
        self.assertTrue(self.logs.lines)
        self._assert_redacted()

    async def test_oneshot_fallback_when_live_session_is_absent(self) -> None:
        condition = await self.client.s05_mqtt_condition("veh-1")
        self.assertEqual(condition["vehicleStatus"]["soc"], 80)
        self.assertEqual(self.broker.peak, 1)
        await self._wait_broker_idle()
        self._assert_redacted()

    async def test_cancel_closes_the_socket(self) -> None:
        await self._start()
        await self.session.wait_until_healthy(3)
        self.assertEqual(self.broker.current, 1)
        assert self.task is not None
        self.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.task
        await asyncio.sleep(0.05)
        self.assertEqual(self.broker.current, 0)
        self.assertFalse(self.session.healthy)
        self._assert_redacted()


class LiveMqttGateTests(unittest.TestCase):
    def test_option_defaults_off_and_ignores_non_mqtt_vehicles(self) -> None:
        self.assertEqual(CONF_ENABLE_LIVE_MQTT, "enable_live_mqtt")
        self.assertEqual(MQTT_LIVE_BACKOFF_SECONDS, (5, 15, 60, 300))
        self.assertFalse(should_start_live_mqtt({}, {"protocolType": "MQTT"}))
        self.assertFalse(should_start_live_mqtt({CONF_ENABLE_LIVE_MQTT: False}, {"protocolType": "MQTT"}))
        self.assertFalse(should_start_live_mqtt({CONF_ENABLE_LIVE_MQTT: True}, {"protocolType": "TLV"}))
        self.assertFalse(should_start_live_mqtt({CONF_ENABLE_LIVE_MQTT: True}, {"protocolType": "SDA-MQTT"}))
        self.assertFalse(should_start_live_mqtt({CONF_ENABLE_LIVE_MQTT: True}, {}))
        self.assertTrue(should_start_live_mqtt({CONF_ENABLE_LIVE_MQTT: True}, {"protocolType": "MQTT"}))
        self.assertTrue(should_start_live_mqtt({CONF_ENABLE_LIVE_MQTT: True}, {"protocolType": "mqtt"}))

    def test_strings_match_and_document_the_option(self) -> None:
        root = Path(__file__).resolve().parents[1] / "custom_components" / "deepal"
        strings = json.loads((root / "strings.json").read_text())
        english = json.loads((root / "translations" / "en.json").read_text())
        self.assertEqual(strings, english)
        described = strings["options"]["step"]["init"]
        self.assertIn("enable_live_mqtt", described["data"])
        self.assertIn("enable_live_mqtt", described["data_description"])
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertIn("enable_live_mqtt", readme)
        self.assertIn("Off by default", described["data_description"]["enable_live_mqtt"])


if __name__ == "__main__":
    unittest.main()
