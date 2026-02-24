import asyncio
import base64
import json
import logging
import random
import threading
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets

from services.time_utils import format_duration


class OneBotClient:
    def __init__(
        self,
        ws_url: str,
        access_token: str,
        target_type: str | None = None,
        target_id: str | None = None,
    ):
        self.ws_url = ws_url
        self.access_token = access_token
        self.target_type = target_type or "group"
        self.target_id = str(target_id) if target_id is not None else ""

        self._loop = None
        self._thread = None
        self._queue = None
        self._queue_ready = threading.Event()
        self._stop = threading.Event()
        self._pending = {}
        self._logger = logging.getLogger("onebot")
        self._reconnect_delay = 1.0
        self._reconnect_max = 60.0
        self._reconnect_factor = 1.7
        self._connected_at = None
        self._stable_seconds = 20.0

    def start(self):
        if not self.ws_url:
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._queue = asyncio.Queue()
        self._queue_ready.set()
        self._loop.create_task(self._runner())
        self._loop.run_forever()

    async def _runner(self):
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        while not self._stop.is_set():
            try:
                ws_url = self._build_ws_url()
                self._logger.info("OneBot WS connecting: %s", ws_url)
                connect_kwargs = {
                    "ping_interval": 20,
                    "ping_timeout": 20,
                }
                if headers:
                    try:
                        async with websockets.connect(
                            ws_url,
                            additional_headers=headers,
                            **connect_kwargs,
                        ) as ws:
                            self._logger.info("OneBot WS connected: %s", ws_url)
                            self._mark_connected()
                            send_task = asyncio.create_task(self._send_loop(ws))
                            recv_task = asyncio.create_task(self._recv_loop(ws))
                            done, pending = await asyncio.wait(
                                [send_task, recv_task],
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for task in pending:
                                task.cancel()
                            await asyncio.gather(*pending, return_exceptions=True)
                    except TypeError:
                        async with websockets.connect(
                            ws_url,
                            extra_headers=headers,
                            **connect_kwargs,
                        ) as ws:
                            self._logger.info("OneBot WS connected: %s", ws_url)
                            self._mark_connected()
                            send_task = asyncio.create_task(self._send_loop(ws))
                            recv_task = asyncio.create_task(self._recv_loop(ws))
                            done, pending = await asyncio.wait(
                                [send_task, recv_task],
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for task in pending:
                                task.cancel()
                            await asyncio.gather(*pending, return_exceptions=True)
                else:
                    async with websockets.connect(ws_url, **connect_kwargs) as ws:
                        self._logger.info("OneBot WS connected: %s", ws_url)
                        self._mark_connected()
                        send_task = asyncio.create_task(self._send_loop(ws))
                        recv_task = asyncio.create_task(self._recv_loop(ws))
                        done, pending = await asyncio.wait(
                            [send_task, recv_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                self._mark_disconnected()
                self._fail_pending("disconnected")
                if not self._stop.is_set():
                    await asyncio.sleep(self._next_reconnect_delay())
            except Exception:
                self._logger.exception("OneBot WS connection error")
                self._mark_disconnected()
                self._fail_pending("disconnected")
                await asyncio.sleep(self._next_reconnect_delay())

    def _resolve_target(self, target_type: str | None, target_id: str | None):
        resolved_type = (target_type or self.target_type or "group").strip() or "group"
        resolved_id = target_id if target_id is not None else self.target_id
        if not resolved_id:
            return None, None
        try:
            resolved_id_int = int(resolved_id)
        except ValueError:
            return None, None
        return resolved_type, resolved_id_int

    async def _send_loop(self, ws):
        while not self._stop.is_set():
            payload = await self._queue.get()
            try:
                await ws.send(json.dumps(payload))
                self._logger.debug("OneBot WS sent: %s", payload.get("action"))
            except Exception as exc:
                try:
                    from websockets.exceptions import ConnectionClosed

                    if isinstance(exc, ConnectionClosed):
                        self._logger.warning(
                            "OneBot WS closed code=%s reason=%s",
                            exc.code,
                            exc.reason,
                        )
                    else:
                        self._logger.exception("OneBot WS send failed")
                except Exception:
                    self._logger.exception("OneBot WS send failed")
                break

    async def _recv_loop(self, ws):
        async for message in ws:
            try:
                data = json.loads(message)
            except Exception:
                self._logger.debug("OneBot WS recv non-json: %s", message)
                continue
            echo = data.get("echo")
            if echo and echo in self._pending:
                future = self._pending.pop(echo)
                if not future.done():
                    future.set_result(data)
            else:
                self._logger.debug("OneBot WS recv event: %s", data.get("post_type"))

    def send_text(
        self, text: str, target_type: str | None = None, target_id: str | None = None
    ):
        if not self.ws_url:
            return
        if not self._queue_ready.wait(timeout=1):
            return
        if not self._loop:
            return

        resolved_type, target = self._resolve_target(target_type, target_id)
        if not target:
            return

        if resolved_type == "private":
            action = "send_private_msg"
            params = {"user_id": target, "message": text}
        else:
            action = "send_group_msg"
            params = {"group_id": target, "message": text}

        payload = {"action": action, "params": params}
        self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)

    def send_segments(
        self, segments: list[dict], target_type: str | None = None, target_id: str | None = None
    ):
        if not self.ws_url:
            return
        if not self._queue_ready.wait(timeout=1):
            return
        if not self._loop:
            return

        resolved_type, target = self._resolve_target(target_type, target_id)
        if not target:
            return

        if resolved_type == "private":
            action = "send_private_msg"
            params = {"user_id": target, "message": segments}
        else:
            action = "send_group_msg"
            params = {"group_id": target, "message": segments}

        payload = {"action": action, "params": params}
        self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)

    def send_image_base64(
        self,
        image_bytes: bytes,
        caption: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
    ):
        if not self.ws_url:
            return
        if not self._queue_ready.wait(timeout=1):
            return
        if not self._loop:
            return

        resolved_type, target = self._resolve_target(target_type, target_id)
        if not target:
            return

        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        segments = []
        if caption:
            segments.append({"type": "text", "data": {"text": caption}})
        segments.append({"type": "image", "data": {"file": f"base64://{image_b64}"}})

        if resolved_type == "private":
            action = "send_private_msg"
            params = {"user_id": target, "message": segments}
        else:
            action = "send_group_msg"
            params = {"group_id": target, "message": segments}

        payload = {"action": action, "params": params}
        self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)

    def send_text_with_result(
        self,
        text: str,
        timeout: int = 5,
        target_type: str | None = None,
        target_id: str | None = None,
    ):
        if not self.ws_url:
            return {"ok": False, "error": "missing_target"}
        if not self._queue_ready.wait(timeout=1):
            return {"ok": False, "error": "queue_not_ready"}
        if not self._loop:
            return {"ok": False, "error": "loop_not_ready"}

        resolved_type, target = self._resolve_target(target_type, target_id)
        if not target:
            return {"ok": False, "error": "invalid_target"}

        if resolved_type == "private":
            action = "send_private_msg"
            params = {"user_id": target, "message": text}
        else:
            action = "send_group_msg"
            params = {"group_id": target, "message": text}

        echo = uuid.uuid4().hex
        payload = {"action": action, "params": params, "echo": echo}
        self._logger.info("OneBot send action=%s target=%s", action, target)
        future = asyncio.run_coroutine_threadsafe(
            self._send_and_wait(payload, timeout),
            self._loop,
        )
        try:
            return future.result(timeout=timeout + 1)
        except Exception:
            self._logger.exception("OneBot send wait timeout")
            return {"ok": False, "error": "timeout"}

    async def _send_and_wait(self, payload, timeout: int):
        echo = payload.get("echo")
        future = self._loop.create_future()
        self._pending[echo] = future
        await self._queue.put(payload)
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
            return {"ok": True, "response": response}
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            return {"ok": False, "error": "timeout"}

    def _fail_pending(self, reason: str):
        if not self._pending:
            return
        self._logger.warning("OneBot pending failed: %s", reason)
        for echo, future in list(self._pending.items()):
            if not future.done():
                future.set_result({"status": "failed", "retcode": -1, "message": reason})
            self._pending.pop(echo, None)

    def _next_reconnect_delay(self) -> float:
        delay = self._reconnect_delay
        jitter = random.uniform(0, max(0.5, delay * 0.1))
        self._reconnect_delay = min(
            self._reconnect_max, self._reconnect_delay * self._reconnect_factor
        )
        return min(self._reconnect_max, delay + jitter)

    def _mark_connected(self):
        self._connected_at = time.monotonic()

    def _mark_disconnected(self):
        if self._connected_at is None:
            return
        alive_for = time.monotonic() - self._connected_at
        self._connected_at = None
        if alive_for >= self._stable_seconds:
            self._reconnect_delay = 1.0

    def send_player_change(
        self,
        server_name: str,
        joined,
        left,
        current_count: int,
        max_count: int,
        durations,
        target_type: str | None = None,
        target_id: str | None = None,
    ):
        if not joined and not left:
            return
        lines = []
        count_text = self._format_count(current_count, max_count)
        for name in joined:
            lines.append(f"{name} 上线了({count_text})")
        for name in left:
            duration = durations.get(name, 0)
            duration_text = format_duration(duration)
            lines.append(f"{name} 下线了({count_text})[在线：{duration_text}]")
        message = f"[{server_name}] " + "，".join(lines)
        self.send_text(message, target_type=target_type, target_id=target_id)

    @staticmethod
    def _format_count(current: int, maximum: int) -> str:
        if maximum and maximum > 0:
            return f"{current}/{maximum}"
        return f"{current}/?"

    def _build_ws_url(self) -> str:
        if not self.access_token:
            return self.ws_url

        parts = urlsplit(self.ws_url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if any(key == "access_token" for key, _ in query):
            return self.ws_url

        query.append(("access_token", self.access_token))
        new_query = urlencode(query, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
