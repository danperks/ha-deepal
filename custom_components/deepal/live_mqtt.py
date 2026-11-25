"""Supervised live MQTT session for S05 vehicles.

The broker allows one connection per ``login_did`` (the MQTT client id). Sunek0's
1.9.0 session is read-only and serves condition, wake, and report waits on that
socket; their door commands are signed REST calls, so they never open a second
client. This integration's S05 lock, window, and boot commands publish on MQTT
with the same ``login_did``. While this session is healthy, polls reuse its
merged snapshot. A command pauses the listener, waits until the socket is
closed, runs the existing one-shot command exchange, then lets the listener
reconnect.

Logs stay free of decrypted payloads, coordinates, tokens, and topic device ids.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import logging
import time
from typing import Any

from .api import (
    DeepalApiError,
    DeepalAuthError,
    DeepalClient,
    _mqtt_connect_packet,
    _mqtt_parse_publish,
    _mqtt_publish_packet,
    _mqtt_read_packet,
    _mqtt_subscribe_packet,
    _s05_condition_from_params,
)
from .const import (
    MQTT_LIVE_BACKOFF_SECONDS,
    MQTT_LIVE_COALESCE_SECONDS,
    MQTT_LIVE_KEEPALIVE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

_PINGREQ = b"\xc0\x00"
_PINGRESP = b"\xd0\x00"


def _puback(packet_id: int) -> bytes:
    return bytes([0x40, 0x02, (packet_id >> 8) & 0xFF, packet_id & 0xFF])


class S05LiveMqttSession:
    """One long-lived MQTT reader for a single ``protocolType == MQTT`` vehicle."""

    def __init__(
        self,
        client: DeepalClient,
        vehicle_id: str,
        on_condition: Callable[[dict[str, Any]], None],
        *,
        refresh_auth: Callable[[], Awaitable[bool]] | None = None,
        keepalive_seconds: float = MQTT_LIVE_KEEPALIVE_SECONDS,
        coalesce_seconds: float = MQTT_LIVE_COALESCE_SECONDS,
        backoff_seconds: Sequence[float] = MQTT_LIVE_BACKOFF_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._vehicle_id = vehicle_id
        self._on_condition = on_condition
        self._refresh_auth = refresh_auth
        self._keepalive_seconds = max(0.05, float(keepalive_seconds))
        self._coalesce_seconds = max(0.0, float(coalesce_seconds))
        self._backoff = tuple(float(item) for item in backoff_seconds) or (5.0,)
        self._sleep = sleep
        self.raw_params: dict[str, Any] = {}
        self._latest: dict[str, Any] | None = None
        self.healthy = False
        self._pause_requested = False
        self._stop = asyncio.Event()
        self._resume_event = asyncio.Event()
        self._wake = asyncio.Event()
        self._healthy_event = asyncio.Event()
        self._condition_event = asyncio.Event()
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task[None] | None = None
        self._flush_handle: asyncio.TimerHandle | None = None
        self._backoff_index = 0
        self._saw_traffic = False
        self._last_tx = 0.0
        self._ping_sent_at = 0.0
        self._attached = False

    @property
    def pause_requested(self) -> bool:
        """Return whether a command has asked this session to drop the socket."""
        return self._pause_requested

    def latest_condition(self) -> dict[str, Any] | None:
        """Return the last merged condition, if a report has been applied."""
        return self._latest

    async def wait_for_condition(self, timeout: float) -> dict[str, Any] | None:
        """Wait until a merged snapshot exists, or return the one already stored."""
        if self._latest is not None:
            return self._latest
        try:
            await asyncio.wait_for(self._condition_event.wait(), timeout)
        except TimeoutError:
            return self._latest
        return self._latest

    async def wait_until_healthy(self, timeout: float = 5) -> None:
        """Block until the broker handshake has succeeded."""
        await asyncio.wait_for(self._healthy_event.wait(), timeout)

    def request_stop(self) -> None:
        """Ask the supervisor to exit and close the socket."""
        self._stop.set()
        self._pause_requested = False
        self._resume_event.set()
        self._wake.set()
        writer = self._writer
        if writer is not None:
            writer.close()

    async def yield_for_exclusive_connect(self) -> None:
        """Close this session's socket and wait until ``login_did`` is free.

        The caller must hold no MQTT socket yet. The cached snapshot is
        dropped so a poll cannot republish pre-command state while this
        socket is down. On failure the pause is cleared so the supervisor
        can reconnect and request a fresh property set.
        """
        self._pause_requested = True
        self._cancel_flush()
        self._latest = None
        self._condition_event.clear()
        self._resume_event.clear()
        self._wake.set()
        try:
            deadline = asyncio.get_running_loop().time() + 20
            while True:
                writer = self._writer
                if writer is not None:
                    writer.close()
                if not self._client.mqtt_socket_active and self._writer is None:
                    return
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise DeepalApiError("S05 live MQTT session did not release the broker in time")
                try:
                    await asyncio.wait_for(self._client.mqtt_socket_idle.wait(), timeout=remaining)
                except TimeoutError as err:
                    raise DeepalApiError("S05 live MQTT session did not release the broker in time") from err
        except BaseException:
            self._pause_requested = False
            self._resume_event.set()
            raise

    def resume_after_exclusive_connect(self) -> None:
        """Allow the supervisor to open the broker again after a command."""
        self._pause_requested = False
        self._resume_event.set()
        self._wake.set()

    async def run(self) -> None:
        """Reconnect until :meth:`request_stop` or task cancellation."""
        self._client.attach_live_mqtt(self)
        self._attached = True
        try:
            while not self._stop.is_set():
                await self._wait_until_resumed()
                if self._stop.is_set():
                    break
                outcome = "eof"
                try:
                    outcome = await self._connect_and_stream()
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    outcome = "error"
                    _LOGGER.warning("Deepal live MQTT session failed (%s)", type(err).__name__)
                if self._stop.is_set() or outcome in ("paused", "skipped") or self._pause_requested:
                    continue
                delay = self._take_delay(reset=self._saw_traffic)
                self._saw_traffic = False
                _LOGGER.warning("Deepal live MQTT session %s; retrying in %ss", outcome, delay)
                await self._interruptible_sleep(delay)
        finally:
            self.healthy = False
            self._healthy_event.clear()
            await self._stop_keepalive()
            await self._close_writer()
            self._cancel_flush()
            if self._client.mqtt_socket_active:
                self._client.mqtt_socket_active = False
                self._client.mqtt_socket_idle.set()
            if self._attached:
                self._client.detach_live_mqtt(self)
                self._attached = False

    async def _wait_until_resumed(self) -> None:
        while self._pause_requested and not self._stop.is_set():
            await self._resume_event.wait()

    def _take_delay(self, *, reset: bool) -> float:
        if reset:
            self._backoff_index = 0
        delay = self._backoff[min(self._backoff_index, len(self._backoff) - 1)]
        if self._backoff_index < len(self._backoff) - 1:
            self._backoff_index += 1
        return delay

    async def _interruptible_sleep(self, delay: float) -> None:
        if self._sleep is not None:
            waiter = asyncio.create_task(self._sleep(delay))
            waker = asyncio.create_task(self._wake.wait())
            _done, pending = await asyncio.wait({waiter, waker}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            self._wake.clear()
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except TimeoutError:
            return
        self._wake.clear()

    async def _fetch_material(self) -> tuple[dict[str, Any], str]:
        config = await self._client._s05_mqtt_config(self._vehicle_id)
        token = await self._client._s05_mqtt_token()
        return config, token

    async def _bootstrap(self) -> tuple[dict[str, Any], str]:
        try:
            return await self._fetch_material()
        except DeepalAuthError:
            if self._refresh_auth is None:
                raise
            refreshed = False
            try:
                refreshed = bool(await self._refresh_auth())
            except Exception as err:
                _LOGGER.warning("Deepal live MQTT token refresh failed (%s)", type(err).__name__)
                raise
            if not refreshed:
                raise
            return await self._fetch_material()

    async def _connect_and_stream(self) -> str:
        if self._stop.is_set() or self._pause_requested:
            return "skipped"
        config, token = await self._bootstrap()
        if self._stop.is_set() or self._pause_requested:
            return "skipped"
        plan = _connection_plan(config)
        async with self._client.mqtt_transport_lock:
            if self._stop.is_set() or self._pause_requested or self._client.mqtt_socket_active:
                return "skipped"
            self._client.mqtt_socket_active = True
            self._client.mqtt_socket_idle.clear()
            try:
                reader, writer = await self._client.open_mqtt_connection(plan["host"], plan["port"])
                self._writer = writer
                await self._handshake(reader, writer, plan, token)
                if self._stop.is_set() or self._pause_requested:
                    await self._close_writer()
                    self._release_socket_flag()
                    return "paused" if self._pause_requested else "skipped"
                self.healthy = True
                self._healthy_event.set()
                self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="deepal-live-mqtt-keepalive")
            except asyncio.CancelledError:
                await self._close_writer()
                self._release_socket_flag()
                raise
            except Exception:
                await self._close_writer()
                self._release_socket_flag()
                raise
        try:
            return await self._read_loop(reader, plan)
        finally:
            self.healthy = False
            self._healthy_event.clear()
            await self._stop_keepalive()
            await self._close_writer()
            self._release_socket_flag()

    def _release_socket_flag(self) -> None:
        self._client.mqtt_socket_active = False
        self._client.mqtt_socket_idle.set()

    async def _handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        plan: dict[str, Any],
        token: str,
    ) -> None:
        advertised = max(1, int(round(self._keepalive_seconds)))
        await self._write(
            _mqtt_connect_packet(plan["login_did"], plan["login_did"], token, keepalive=advertised)
        )
        first, body = await asyncio.wait_for(_mqtt_read_packet(reader), timeout=15)
        rc = body[1] if first == 0x20 and len(body) >= 2 else None
        if rc != 0:
            raise DeepalApiError(f"S05 MQTT broker rejected connection: rc={rc}")
        topics = sorted(set(plan["topics"]))
        await self._write(_mqtt_subscribe_packet(1, topics))
        await asyncio.wait_for(_mqtt_read_packet(reader), timeout=15)
        await self._write(_mqtt_publish_packet(plan["login_pub"], DeepalClient._s05_login_payload(plan["login_did"])))

    async def _read_loop(self, reader: asyncio.StreamReader, plan: dict[str, Any]) -> str:
        secret_key: str | None = None
        while not self._stop.is_set():
            if self._pause_requested:
                return "paused"
            try:
                first, body = await _mqtt_read_packet(reader)
            except asyncio.CancelledError:
                raise
            except (asyncio.IncompleteReadError, ConnectionError, OSError, TimeoutError):
                if self._pause_requested or self._stop.is_set():
                    return "paused"
                return "eof"
            try:
                secret_key = await self._handle_packet(first, body, plan, secret_key)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError):
                if self._pause_requested or self._stop.is_set():
                    return "paused"
                return "eof"
        return "paused"

    async def _handle_packet(
        self,
        first: int,
        body: bytes,
        plan: dict[str, Any],
        secret_key: str | None,
    ) -> str | None:
        packet_type = first >> 4
        if packet_type == 12:
            await self._write(_PINGRESP)
            return secret_key
        if packet_type == 13:
            self._ping_sent_at = 0.0
            self._saw_traffic = True
            return secret_key
        if packet_type != 3:
            return secret_key
        try:
            _topic, payload, packet_id = _mqtt_parse_publish(first, body)
        except (ValueError, UnicodeError, KeyError):
            return secret_key
        if packet_id is not None:
            await self._write(_puback(packet_id))
        if not isinstance(payload, dict):
            return secret_key
        if secret_key is None:
            secret_key = DeepalClient._s05_secret_from_payload(payload)
            if secret_key:
                req_id = DeepalClient._s05_req_id(plan["device_did"])
                await self._write(
                    _mqtt_publish_packet(
                        plan["properties_get"],
                        DeepalClient._s05_condition_request_payload(
                            plan["device_did"], plan["login_did"], secret_key, req_id
                        ),
                    )
                )
            return secret_key
        params = DeepalClient._s05_condition_params_from_payload(payload, secret_key)
        if params:
            self._merge(params)
        return secret_key

    def _merge(self, params: dict[str, Any]) -> None:
        """Fold a partial property report onto the retained parameter map."""
        self.raw_params.update(params)
        self._saw_traffic = True
        if self._flush_handle is not None:
            return
        self._flush_handle = asyncio.get_running_loop().call_later(self._coalesce_seconds, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        if self._pause_requested or self._stop.is_set() or not self.raw_params:
            return
        condition = _s05_condition_from_params(dict(self.raw_params))
        self._latest = condition
        self._condition_event.set()
        try:
            self._on_condition(condition)
        except Exception as err:
            _LOGGER.debug("Deepal live MQTT condition callback failed (%s)", type(err).__name__)

    def _cancel_flush(self) -> None:
        handle = self._flush_handle
        self._flush_handle = None
        if handle is not None:
            handle.cancel()

    async def _keepalive_loop(self) -> None:
        interval = max(0.05, self._keepalive_seconds / 2)
        while not self._stop.is_set() and not self._pause_requested:
            await asyncio.sleep(interval)
            if self._writer is None or self._stop.is_set() or self._pause_requested:
                return
            if self._ping_sent_at and time.monotonic() - self._ping_sent_at > self._keepalive_seconds:
                writer = self._writer
                if writer is not None:
                    writer.close()
                return
            if time.monotonic() - self._last_tx < interval:
                continue
            self._ping_sent_at = time.monotonic()
            try:
                await self._write(_PINGREQ)
            except (ConnectionError, OSError, RuntimeError):
                return

    async def _write(self, data: bytes) -> None:
        async with self._write_lock:
            writer = self._writer
            if writer is None:
                raise ConnectionError("closed")
            writer.write(data)
            await writer.drain()
            self._last_tx = time.monotonic()

    async def _stop_keepalive(self) -> None:
        task = self._keepalive_task
        self._keepalive_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _close_writer(self) -> None:
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            return


def _connection_plan(config: dict[str, Any]) -> dict[str, Any]:
    """Pick the broker and the login plus property topics from ``getConnConf``."""
    info = ((config.get("mqttConnectionInfos") or [None])[0]) or {}
    cluster = ((info.get("clusterInfos") or [None])[0]) or {}
    host = str(cluster.get("brokerUrl") or "").replace("ssl://", "")
    port = int(cluster.get("brokerPort") or 8883)
    topics: list[str] = []
    login_pub: str | None = None
    login_did: str | None = None
    properties_get: str | None = None
    device_did: str | None = None
    for topic_info in info.get("topicInfos") or []:
        if not isinstance(topic_info, dict):
            continue
        msg_type = topic_info.get("msgType")
        for topic in topic_info.get("pubTopics") or []:
            if not isinstance(topic, str):
                continue
            if msg_type == "loginout" and "/loginout/req" in topic:
                login_pub = topic
                login_did = DeepalClient._s05_topic_did(topic)
            if msg_type == "properties" and "/properties/get/req" in topic:
                properties_get = topic
                device_did = DeepalClient._s05_topic_did(topic)
        for topic in topic_info.get("subTopics") or []:
            if not isinstance(topic, str):
                continue
            if msg_type in ("loginout", "properties") and "/commands/" not in topic and "/set/" not in topic:
                topics.append(topic)
            if device_did is None and "/properties/" in topic:
                device_did = DeepalClient._s05_topic_did(topic)
    if not host or not login_pub or not login_did or not properties_get or not device_did or not topics:
        raise DeepalApiError("S05 MQTT config did not include required topics")
    return {
        "host": host,
        "port": port,
        "topics": topics,
        "login_pub": login_pub,
        "login_did": login_did,
        "properties_get": properties_get,
        "device_did": device_did,
    }
