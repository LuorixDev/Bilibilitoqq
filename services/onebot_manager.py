import threading

from services.onebot_client import OneBotClient


class OneBotManager:
    def __init__(self, default_settings: dict):
        self._default = default_settings
        self._clients = {}
        self._lock = threading.Lock()

    def start(self):
        # Clients are started on demand.
        return

    def resolve_settings(self, settings: dict) -> dict:
        return {
            "ws_url": settings.get("onebot_ws_url") or self._default.get("ws_url", ""),
            "access_token": settings.get("onebot_access_token") or self._default.get(
                "access_token", ""
            ),
            "target_type": settings.get("onebot_target_type")
            or self._default.get("target_type", "group"),
            "target_id": settings.get("onebot_target_id") or self._default.get("target_id", ""),
        }

    def _get_client(self, resolved: dict):
        ws_url = resolved.get("ws_url")
        target_id = resolved.get("target_id")
        if not ws_url or not target_id:
            return None

        key = (
            ws_url,
            resolved.get("access_token") or "",
            resolved.get("target_type") or "group",
            str(target_id),
        )
        with self._lock:
            client = self._clients.get(key)
            if not client:
                client = OneBotClient(
                    ws_url=ws_url,
                    access_token=resolved.get("access_token") or "",
                    target_type=resolved.get("target_type") or "group",
                    target_id=str(target_id),
                )
                client.start()
                self._clients[key] = client
        return client

    def send_text(self, settings: dict, text: str):
        resolved = self.resolve_settings(settings)
        client = self._get_client(resolved)
        if client:
            client.send_text(text)
        return None

    def send_image_base64(self, settings: dict, image_bytes: bytes, caption: str | None = None):
        resolved = self.resolve_settings(settings)
        client = self._get_client(resolved)
        if client:
            client.send_image_base64(image_bytes, caption)
        return None

    def send_segments(self, settings: dict, segments: list[dict]):
        resolved = self.resolve_settings(settings)
        client = self._get_client(resolved)
        if client:
            client.send_segments(segments)
        return None

    def send_text_with_result(self, settings: dict, text: str, timeout: int = 5):
        resolved = self.resolve_settings(settings)
        client = self._get_client(resolved)
        if not client:
            return {"ok": False, "error": "missing_target"}
        return client.send_text_with_result(text, timeout=timeout)

    def send_player_change(
        self,
        settings: dict,
        server_name: str,
        joined,
        left,
        current_count: int,
        max_count: int,
        durations,
    ):
        resolved = self.resolve_settings(settings)
        client = self._get_client(resolved)
        if client:
            client.send_player_change(
                server_name,
                joined,
                left,
                current_count,
                max_count,
                durations,
            )
