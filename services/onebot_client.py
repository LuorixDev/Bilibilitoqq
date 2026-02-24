import asyncio
import base64
import json
import logging
import threading
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets

from services.time_utils import format_duration


class OneBotClient:
    def __init__(self, ws_url: str, access_token: str, target_type: str, target_id: str):
        self.ws_url = ws_url
        self.access_token = access_token
        self.target_type = target_type
        self.target_id = target_id

        self._loop = None
        self._thread = None
        self._queue = None
        self._queue_ready = threading.Event()
        self._stop = threading.Event()
        self._pending = {}
        self._logger = logging.getLogger("onebot")

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
                        send_task = asyncio.create_task(self._send_loop(ws))
                        recv_task = asyncio.create_task(self._recv_loop(ws))
                        done, pending = await asyncio.wait(
                            [send_task, recv_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                self._fail_pending("disconnected")
            except Exception:
                self._logger.exception("OneBot WS connection error")
                self._fail_pending("disconnected")
                await asyncio.sleep(5)

    async def _send_loop(self, ws):
        while not self._stop.is_set():
            payload = await self._queue.get()
            try:
                await ws.send(json.dumps(payload))
                self._logger.debug("OneBot WS sent: %s", payload.get("action"))
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

    def send_text(self, text: str):
        if not self.ws_url or not self.target_id:
            return
        if not self._queue_ready.wait(timeout=1):
            return
        if not self._loop:
            return

        try:
            target = int(self.target_id)
        except ValueError:
            return

        if self.target_type == "private":
            action = "send_private_msg"
            params = {"user_id": target, "message": text}
        else:
            action = "send_group_msg"
            params = {"group_id": target, "message": text}

        payload = {"action": action, "params": params}
        self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)

    def send_segments(self, segments: list[dict]):
        if not self.ws_url or not self.target_id:
            return
        if not self._queue_ready.wait(timeout=1):
            return
        if not self._loop:
            return

        try:
            target = int(self.target_id)
        except ValueError:
            return

        if self.target_type == "private":
            action = "send_private_msg"
            params = {"user_id": target, "message": segments}
        else:
            action = "send_group_msg"
            params = {"group_id": target, "message": segments}

        payload = {"action": action, "params": params}
        self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)

    def send_image_base64(self, image_bytes: bytes, caption: str | None = None):
        if not self.ws_url or not self.target_id:
            return
        if not self._queue_ready.wait(timeout=1):
            return
        if not self._loop:
            return

        try:
            target = int(self.target_id)
        except ValueError:
            return

        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        segments = []
        if caption:
            segments.append({"type": "text", "data": {"text": caption}})
        segments.append({"type": "image", "data": {"file": f"base64://{image_b64}"}})

        if self.target_type == "private":
            action = "send_private_msg"
            params = {"user_id": target, "message": segments}
        else:
            action = "send_group_msg"
            params = {"group_id": target, "message": segments}

        payload = {"action": action, "params": params}
        self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)

    def send_text_with_result(self, text: str, timeout: int = 5):
        if not self.ws_url or not self.target_id:
            return {"ok": False, "error": "missing_target"}
        if not self._queue_ready.wait(timeout=1):
            return {"ok": False, "error": "queue_not_ready"}
        if not self._loop:
            return {"ok": False, "error": "loop_not_ready"}

        try:
            target = int(self.target_id)
        except ValueError:
            return {"ok": False, "error": "invalid_target"}

        if self.target_type == "private":
            action = "send_private_msg"
            params = {"user_id": target, "message": text}
        else:
            action = "send_group_msg"
            params = {"group_id": target, "message": text}

        echo = uuid.uuid4().hex
        payload = {"action": action, "params": params, "echo": echo}
        self._logger.info("OneBot send action=%s target=%s", action, self.target_id)
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

    def send_player_change(
        self,
        server_name: str,
        joined,
        left,
        current_count: int,
        max_count: int,
        durations,
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
        self.send_text(message)

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
