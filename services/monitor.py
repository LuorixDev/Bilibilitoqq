import base64
import html as html_lib
import logging
import re
import threading
import time
from datetime import datetime

from config import (
    DYNAMIC_SCREENSHOT_FULL_PAGE,
    DYNAMIC_SCREENSHOT_WAIT,
    MAX_DYNAMIC_PER_POLL,
    POLL_INTERVAL,
)
from models import BiliUser
from services.bili_api import (
    download_image,
    fetch_dynamic_list,
    fetch_live_info,
    fetch_user_info,
)
from services.html_screenshot import render_html_to_image
from services.message_templates import DEFAULT_TEMPLATES
from services.screenshot_store import get_screenshot_template_value
from services.settings import get_global_poll_interval, get_live_hourly_interval_minutes
from services.screenshot_templates import DEFAULT_HTML_TEMPLATES, render_html_template
from services.state import update_status
from services.time_utils import format_duration

_SPECIAL_PATTERN = re.compile(r"(\{SHOTPICTURE\}|\[atALL\])")


class BiliMonitor:
    def __init__(self, app, onebot, onebot_defaults: dict):
        self.app = app
        self.onebot = onebot
        self._onebot_defaults = onebot_defaults
        self._thread = None
        self._stop = threading.Event()
        self._logger = logging.getLogger("bili_monitor")

        self._last_dynamic_id = {}
        self._last_dynamic_text = {}
        self._last_dynamic_time = {}
        self._last_dynamic_url = {}
        self._last_dynamic_title = {}
        self._last_dynamic_type = {}
        self._last_dynamic_video_url = {}
        self._last_dynamic_cover = {}
        self._last_dynamic_is_video = {}
        self._user_face = {}
        self._next_poll_time = {}
        self._last_live_status = {}
        self._live_started_at = {}
        self._live_last_hourly = {}
        self._live_max_online = {}
        self._live_current_online = {}
        self._live_title = {}
        self._live_url = {}

    def reset_user_state(self, uid: str | int):
        if uid is None:
            return
        uid = str(uid)
        self._last_dynamic_id.pop(uid, None)
        self._last_dynamic_text.pop(uid, None)
        self._last_dynamic_time.pop(uid, None)
        self._last_dynamic_url.pop(uid, None)
        self._last_dynamic_title.pop(uid, None)
        self._last_dynamic_type.pop(uid, None)
        self._last_dynamic_video_url.pop(uid, None)
        self._last_dynamic_cover.pop(uid, None)
        self._last_dynamic_is_video.pop(uid, None)
        self._user_face.pop(uid, None)
        self._last_live_status.pop(uid, None)
        self._live_started_at.pop(uid, None)
        for key in list(self._live_last_hourly.keys()):
            if key == uid:
                self._live_last_hourly.pop(key, None)
            elif isinstance(key, tuple) and key and str(key[0]) == uid:
                self._live_last_hourly.pop(key, None)
        self._live_max_online.pop(uid, None)
        self._live_current_online.pop(uid, None)
        self._live_title.pop(uid, None)
        self._live_url.pop(uid, None)
        self._next_poll_time.pop(uid, None)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            sleep_for = self._poll_once()
            if sleep_for is None:
                sleep_for = POLL_INTERVAL
            sleep_for = max(1.0, float(sleep_for))
            time.sleep(sleep_for)

    def _poll_once(self, force: bool = False):
        with self.app.app_context():
            global_interval = get_global_poll_interval()
            live_hourly_default = max(30, get_live_hourly_interval_minutes())
            now = time.time()
            users = BiliUser.query.filter_by(enabled=True).all()
            users = [
                {
                    "id": u.id,
                    "uid": u.uid,
                    "name": u.name,
                    "poll_interval": u.poll_interval or 0,
                    "global_poll_interval": global_interval,
                    "live_hourly_default": live_hourly_default,
                    "credential": {
                        "cookie": u.cookie,
                        "sessdata": u.sessdata,
                        "bili_jct": u.bili_jct,
                        "buvid3": u.buvid3,
                        "buvid4": u.buvid4,
                        "dedeuserid": u.dedeuserid,
                        "ac_time_value": u.ac_time_value,
                    },
                    "bindings": [
                        {
                            "id": b.id,
                            "name": b.name,
                            "onebot_profile": {
                                "ws_url": b.onebot_profile.ws_url,
                                "access_token": b.onebot_profile.access_token,
                                "name": b.onebot_profile.name,
                            }
                            if b.onebot_profile
                            else None,
                            "onebot_ws_url": b.onebot_ws_url,
                            "onebot_access_token": b.onebot_access_token,
                            "onebot_target_type": b.onebot_target_type,
                            "onebot_target_id": b.onebot_target_id,
                            "enable_onebot": b.enable_onebot,
                            "notify_dynamic": b.notify_dynamic,
                            "notify_video": b.notify_video,
                            "notify_live_start": b.notify_live_start,
                            "notify_live_hourly": b.notify_live_hourly,
                            "notify_live_end": b.notify_live_end,
                            "live_hourly_interval": b.live_hourly_interval,
                            "enable_screenshot": b.enable_screenshot,
                            "template_dynamic": b.template_dynamic,
                            "template_video": b.template_video,
                            "template_live_start": b.template_live_start,
                            "template_live_hourly": b.template_live_hourly,
                            "template_live_end": b.template_live_end,
                        }
                        for b in u.bindings
                    ],
                }
                for u in users
            ]

        self._logger.debug("poll start users=%s", len(users))

        for user in users:
            uid = user["uid"]
            bindings = user.get("bindings") or []
            if not bindings:
                continue
            interval = self._resolve_poll_interval(user)
            if not force:
                next_time = self._next_poll_time.get(uid)
                if next_time and now < next_time:
                    continue
            self._next_poll_time[uid] = now + interval
            self._logger.debug(
                "poll user uid=%s bindings=%s", uid, len(bindings), extra={"uid": uid}
            )

            name = user.get("name") or ""
            if not name:
                info = fetch_user_info(uid, user.get("credential"))
                if info:
                    name = info.get("name") or info.get("uname") or ""
                    if name:
                        self._update_user_name(user["id"], name)
                if not name:
                    name = f"UID {uid}"

            self._ensure_user_profile(user, name)
            self._handle_dynamic(user, name)
            self._handle_live(user, name)
            self._update_status_cache(user, name)
        return self._next_sleep_time(now)

    @staticmethod
    def _resolve_poll_interval(user: dict) -> int:
        value = user.get("poll_interval") or 0
        try:
            value = int(value)
        except Exception:
            value = 0
        global_interval = user.get("global_poll_interval") or 0
        try:
            global_interval = int(global_interval)
        except Exception:
            global_interval = 0
        if global_interval <= 0:
            global_interval = get_global_poll_interval()
        if value <= 0:
            return global_interval
        return value

    def _next_sleep_time(self, now: float) -> float:
        if not self._next_poll_time:
            return POLL_INTERVAL
        next_time = min(self._next_poll_time.values())
        remaining = next_time - now
        if remaining <= 0:
            return 1.0
        return min(POLL_INTERVAL, remaining)

    def _update_user_name(self, user_id: int, name: str):
        try:
            with self.app.app_context():
                target = BiliUser.query.get(user_id)
                if target and target.name != name:
                    target.name = name
                    from models import db

                    db.session.commit()
        except Exception:
            self._logger.exception("Failed to update user name")

    def _handle_dynamic(self, user: dict, name: str):
        uid = user["uid"]
        items = fetch_dynamic_list(uid, credential_data=user.get("credential"))
        if not items:
            return
        self._logger.debug(
            "dynamic list uid=%s items=%s", uid, len(items), extra={"uid": uid}
        )

        latest_id = self._get_dynamic_id(items[0])
        if not latest_id:
            return
        self._logger.debug(
            "dynamic latest uid=%s id=%s", uid, latest_id, extra={"uid": uid}
        )

        last_id = self._last_dynamic_id.get(uid)
        if not last_id:
            self._last_dynamic_id[uid] = latest_id
            info = self._parse_dynamic(items[0])
            if info:
                self._cache_last_dynamic(uid, info)
            return

        if last_id == latest_id:
            return

        new_items = []
        for item in items:
            if self._get_dynamic_id(item) == last_id:
                break
            new_items.append(item)
        if not new_items:
            self._last_dynamic_id[uid] = latest_id
            return

        if len(new_items) > MAX_DYNAMIC_PER_POLL:
            new_items = new_items[:MAX_DYNAMIC_PER_POLL]

        new_items.reverse()
        for item in new_items:
            info = self._parse_dynamic(item)
            if not info:
                continue
            if not info.get("avatar"):
                info["avatar"] = self._user_face.get(uid, "")
            self._dispatch_dynamic(user, name, info)

        self._last_dynamic_id[uid] = latest_id
        latest_info = self._parse_dynamic(items[0])
        if latest_info:
            if not latest_info.get("avatar"):
                latest_info["avatar"] = self._user_face.get(uid, "")
            self._cache_last_dynamic(uid, latest_info)

    def _dispatch_dynamic(self, user: dict, name: str, info: dict):
        bindings = user.get("bindings") or []
        is_video = info.get("is_video")
        html_values = self._dynamic_html_values(name, info)
        for binding in bindings:
            if not self._onebot_enabled(binding):
                continue

            if is_video and binding.get("notify_video"):
                template = self._get_template(binding, "video")
                values = self._video_values(name, info)
                image_bytes = self._maybe_render_dynamic_image(
                    binding, template, html_values, info, user.get("credential")
                )
                self._send_template(binding, template, values, image_bytes)
                continue

            if not is_video and binding.get("notify_dynamic"):
                template = self._get_template(binding, "dynamic")
                values = self._dynamic_values(name, info)
                image_bytes = self._maybe_render_dynamic_image(
                    binding, template, html_values, info, user.get("credential")
                )
                self._send_template(binding, template, values, image_bytes)
                continue

            if is_video and binding.get("notify_dynamic") and not binding.get("notify_video"):
                template = self._get_template(binding, "dynamic")
                values = self._dynamic_values(name, info)
                image_bytes = self._maybe_render_dynamic_image(
                    binding, template, html_values, info, user.get("credential")
                )
                self._send_template(binding, template, values, image_bytes)

    def _handle_live(self, user: dict, name: str):
        uid = user["uid"]
        bindings = user.get("bindings") or []
        info = fetch_live_info(uid, user.get("credential"))
        if not info:
            return

        live_status = info.get("liveStatus")
        if live_status is None:
            live_status = info.get("live_status")
        is_live = str(live_status) == "1"

        room_id = info.get("roomid") or info.get("room_id")
        title = info.get("title") or info.get("roomname") or ""
        online = info.get("online") or info.get("online_num")
        live_url = f"https://live.bilibili.com/{room_id}" if room_id else ""

        cover_url = (
            info.get("keyframe")
            or info.get("live_screen")
            or info.get("cover")
            or info.get("cover_from_user")
            or info.get("user_cover")
            or ""
        )

        self._logger.debug(
            "live status uid=%s live=%s online=%s title=%s",
            uid,
            is_live,
            online,
            title,
            extra={"uid": uid},
        )

        if online is not None:
            self._live_current_online[uid] = self._safe_int(online)

        last_live = self._last_live_status.get(uid)
        now = time.time()
        if last_live is None:
            self._last_live_status[uid] = is_live
            if is_live:
                self._live_started_at[uid] = now
                self._live_max_online[uid] = self._safe_int(online)
                self._live_current_online[uid] = self._safe_int(online)
                self._live_title[uid] = title
                self._live_url[uid] = live_url
                for binding in bindings:
                    if not self._onebot_enabled(binding):
                        continue
                    if not self._notify_live_hourly(binding):
                        continue
                    binding_id = binding.get("id")
                    if binding_id is None:
                        continue
                    self._live_last_hourly[(uid, binding_id)] = now
            return

        if is_live and not last_live:
            self._live_started_at[uid] = now
            self._live_max_online[uid] = self._safe_int(online)
            self._live_current_online[uid] = self._safe_int(online)
            self._live_title[uid] = title
            self._live_url[uid] = live_url
            for binding in bindings:
                if not self._onebot_enabled(binding):
                    continue
                if not self._notify_live_hourly(binding):
                    continue
                binding_id = binding.get("id")
                if binding_id is None:
                    continue
                self._live_last_hourly[(uid, binding_id)] = now
            self._dispatch_live_event(
                user,
                name,
                "live_start",
                title,
                online,
                live_url,
                0,
                None,
                cover_url,
            )

        if is_live and last_live:
            if online is not None:
                self._live_max_online[uid] = max(
                    self._live_max_online.get(uid, 0), self._safe_int(online)
                )
                self._live_current_online[uid] = self._safe_int(online)
            for binding in bindings:
                if not self._onebot_enabled(binding):
                    continue
                if not self._notify_live_hourly(binding):
                    continue
                binding_id = binding.get("id")
                if binding_id is None:
                    continue
                default_minutes = user.get("live_hourly_default") or 60
                try:
                    interval_minutes = int(binding.get("live_hourly_interval") or 0)
                except Exception:
                    interval_minutes = 0
                if interval_minutes <= 0:
                    try:
                        interval_minutes = int(default_minutes)
                    except Exception:
                        interval_minutes = 60
                if interval_minutes < 30:
                    interval_minutes = 30
                live_interval = interval_minutes * 60
                key = (uid, binding_id)
                last_hourly = self._live_last_hourly.get(key)
                if not last_hourly:
                    self._live_last_hourly[key] = now
                    continue
                if now - last_hourly >= live_interval:
                    start_ts = self._live_started_at.get(uid)
                    if not start_ts:
                        api_start = info.get("live_time") or info.get("start_time") or 0
                        if api_start:
                            try:
                                start_ts = float(api_start)
                            except Exception:
                                start_ts = None
                    duration = now - start_ts if start_ts else 0
                    max_online = self._live_max_online.get(uid, 0)
                    self._dispatch_live_event(
                        {"uid": uid, "bindings": [binding]},
                        name,
                        "live_hourly",
                        title,
                        online,
                        live_url,
                        duration,
                        max_online,
                        cover_url,
                    )
                    self._live_last_hourly[key] = now

        if not is_live and last_live:
            start_ts = self._live_started_at.get(uid)
            duration = time.time() - start_ts if start_ts else 0
            max_online = self._live_max_online.get(uid, 0)
            self._dispatch_live_event(
                user,
                name,
                "live_end",
                title,
                online,
                live_url,
                duration,
                max_online,
                cover_url,
            )
            self._live_started_at.pop(uid, None)
            for key in list(self._live_last_hourly.keys()):
                if key == uid:
                    self._live_last_hourly.pop(key, None)
                elif isinstance(key, tuple) and key and str(key[0]) == str(uid):
                    self._live_last_hourly.pop(key, None)
            self._live_max_online.pop(uid, None)
            self._live_current_online.pop(uid, None)
            self._live_title.pop(uid, None)
            self._live_url.pop(uid, None)

        self._last_live_status[uid] = is_live
        if is_live:
            self._live_title[uid] = title or self._live_title.get(uid, "")
            if live_url:
                self._live_url[uid] = live_url

    def _dispatch_live_event(
        self,
        user: dict,
        name: str,
        event_key: str,
        title: str,
        online,
        live_url: str,
        duration: float | None,
        max_online: int | None,
        cover_url: str,
    ):
        avatar = self._user_face.get(user.get("uid"), "")
        html_values = self._live_html_values(
            name,
            title,
            online,
            live_url,
            duration,
            max_online,
            cover_url,
            avatar,
        )
        for binding in user.get("bindings") or []:
            if not self._onebot_enabled(binding):
                continue
            if event_key == "live_start" and not self._notify_live_start(binding):
                continue
            if event_key == "live_hourly" and not self._notify_live_hourly(binding):
                continue
            if event_key == "live_end" and not self._notify_live_end(binding):
                continue
            template = self._get_template(binding, event_key)
            values = self._live_values(name, title, online, live_url, duration, max_online)
            image_bytes = self._maybe_render_live_image(
                binding, template, html_values, cover_url
            )
            self._send_template(binding, template, values, image_bytes)

    def _get_dynamic_image(self, info: dict, credential: dict | None) -> bytes | None:
        cover_url = info.get("cover_url") or ""
        if cover_url:
            image = download_image(cover_url)
            if image:
                return image
        url = info.get("url")
        if not url:
            return None
        cookie = self._build_cookie_header(credential)
        return self._capture_dynamic_screenshot(url, cookie)

    def _maybe_render_dynamic_image(
        self,
        binding: dict,
        template: str,
        html_values: dict,
        info: dict,
        credential: dict | None,
    ) -> bytes | None:
        if not self._onebot_enabled(binding):
            return None
        if not binding.get("enable_screenshot"):
            return None
        if "{SHOTPICTURE}" not in template:
            return None
        html_template = self._get_html_template(binding, "dynamic")
        image = self._render_html_image(html_template, html_values)
        if image:
            return image
        return None

    def _maybe_render_live_image(
        self, binding: dict, template: str, html_values: dict, cover_url: str
    ) -> bytes | None:
        if not self._onebot_enabled(binding):
            return None
        if not binding.get("enable_screenshot"):
            return None
        if "{SHOTPICTURE}" not in template:
            return None
        html_template = self._get_html_template(binding, "live")
        image = self._render_html_image(html_template, html_values)
        if image:
            return image
        if not cover_url:
            return None
        return download_image(cover_url)

    def _send_template(self, binding: dict, template: str, values: dict, image_bytes: bytes | None):
        if not self._onebot_enabled(binding):
            return
        if not binding.get("enable_screenshot"):
            image_bytes = None

        segments, rich = self._build_segments(template, values, image_bytes)
        if not segments:
            return
        settings = self._settings_for_binding(binding)
        if rich:
            self.onebot.send_segments(settings, segments)
            return
        text = "".join(seg["data"]["text"] for seg in segments if seg.get("type") == "text")
        if text:
            self.onebot.send_text(settings, text)

    def _build_segments(self, template: str, values: dict, image_bytes: bytes | None):
        parts = _SPECIAL_PATTERN.split(template)
        segments = []
        rich = False
        for part in parts:
            if not part:
                continue
            if part == "{SHOTPICTURE}":
                if image_bytes:
                    segments.append(self._image_segment(image_bytes))
                    rich = True
                continue
            if part == "[atALL]":
                segments.append({"type": "at", "data": {"qq": "all"}})
                rich = True
                continue

            text = self._apply_values(part, values)
            if text:
                segments.append({"type": "text", "data": {"text": text}})
        if any(seg.get("type") == "at" for seg in segments):
            rich = True
        if any(seg.get("type") == "image" for seg in segments):
            rich = True
        return segments, rich

    def _apply_values(self, text: str, values: dict) -> str:
        if not text:
            return ""
        for key, value in values.items():
            text = text.replace(f"{{{key}}}", "" if value is None else str(value))
        return text

    def _image_segment(self, image_bytes: bytes) -> dict:
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        return {"type": "image", "data": {"file": f"base64://{image_b64}"}}

    def _get_template(self, binding: dict, key: str) -> str:
        value = binding.get(f"template_{key}")
        if value:
            return value
        return DEFAULT_TEMPLATES.get(key, "")

    def _dynamic_values(self, name: str, info: dict) -> dict:
        return {
            "name": name,
            "text": info.get("text") or "",
            "url": info.get("url") or "",
            "title": info.get("video_title") or "",
        }

    def _video_values(self, name: str, info: dict) -> dict:
        return {
            "name": name,
            "title": info.get("video_title") or "",
            "url": info.get("video_url") or info.get("url") or "",
            "text": info.get("text") or "",
        }

    def _live_values(
        self,
        name: str,
        title: str,
        online,
        url: str,
        duration: float | None,
        max_online: int | None,
    ) -> dict:
        duration_text = format_duration(duration) if duration is not None else ""
        return {
            "name": name,
            "title": title or "",
            "online": online if online is not None else "",
            "url": url or "",
            "duration": duration_text,
            "max_online": max_online if max_online is not None else "",
        }

    def _dynamic_html_values(self, name: str, info: dict) -> dict:
        is_video = bool(info.get("is_video"))
        title = info.get("video_title") or ""
        if not title and not is_video:
            title = "发布了新动态"
        url = info.get("video_url") or info.get("url") or ""
        images = info.get("images") or []
        extra = info.get("extra") or {}
        media_html = info.get("media_html") or self._build_media_html(images, extra)
        cover = ""
        if not media_html:
            cover = info.get("cover_url") or (images[0] if images else "")
        text_html = info.get("text_html")
        if not text_html:
            text_plain = info.get("text") or ""
            text_html = html_lib.escape(text_plain).replace("\n", "<br>")
        avatar = info.get("avatar") or ""
        return {
            "name": name,
            "name_initial": self._name_initial(name),
            "text": info.get("text") or "",
            "text_html": text_html,
            "title": title,
            "url": url,
            "online": "",
            "duration": "",
            "max_online": "",
            "cover": cover,
            "cover_display": "block" if cover else "none",
            "avatar": avatar,
            "avatar_display": "block" if avatar else "none",
            "avatar_text_display": "none" if avatar else "block",
            "image_count": len(images),
            "media_html": media_html,
        }

    def _live_html_values(
        self,
        name: str,
        title: str,
        online,
        live_url: str,
        duration: float | None,
        max_online: int | None,
        cover_url: str,
        avatar: str,
    ) -> dict:
        live_values = self._live_values(name, title, online, live_url, duration, max_online)
        cover = cover_url or ""
        return {
            "name": live_values.get("name"),
            "name_initial": self._name_initial(name),
            "text": "",
            "title": live_values.get("title"),
            "url": live_values.get("url"),
            "online": live_values.get("online"),
            "duration": live_values.get("duration"),
            "max_online": live_values.get("max_online"),
            "cover": cover,
            "cover_display": "block" if cover else "none",
            "avatar": avatar or "",
            "avatar_display": "block" if avatar else "none",
            "avatar_text_display": "none" if avatar else "block",
            "image_count": 0,
            "media_html": "",
        }

    def _get_html_template(self, binding: dict, key: str) -> str:
        binding_id = binding.get("id")
        if binding_id:
            return get_screenshot_template_value(int(binding_id), key)
        return DEFAULT_HTML_TEMPLATES.get(key, "")

    def _render_html_image(self, template: str, values: dict) -> bytes | None:
        if not template:
            return None
        html = render_html_template(template, values)
        return render_html_to_image(html)

    def _update_status_cache(self, user: dict, name: str):
        uid = user["uid"]
        live_duration = ""
        if self._last_live_status.get(uid):
            start_ts = self._live_started_at.get(uid)
            if start_ts:
                live_duration = format_duration(time.time() - start_ts)
        interval = self._resolve_poll_interval(user)
        next_time = self._next_poll_time.get(uid)
        if not next_time:
            next_time = time.time() + interval
        status = {
            "id": user["id"],
            "uid": uid,
            "name": name,
            "live": bool(self._last_live_status.get(uid)),
            "live_title": self._live_title.get(uid) or "",
            "live_online": self._live_current_online.get(uid)
            if self._last_live_status.get(uid)
            else None,
            "live_duration": live_duration,
            "live_url": self._live_url.get(uid) or "",
            "last_dynamic_text": self._last_dynamic_text.get(uid) or "",
            "last_dynamic_time": self._last_dynamic_time.get(uid),
            "last_dynamic_url": self._last_dynamic_url.get(uid) or "",
            "last_dynamic_id": self._last_dynamic_id.get(uid) or "",
            "last_dynamic_title": self._last_dynamic_title.get(uid) or "",
            "last_dynamic_type": self._last_dynamic_type.get(uid) or "",
            "last_dynamic_video_url": self._last_dynamic_video_url.get(uid) or "",
            "last_dynamic_cover": self._last_dynamic_cover.get(uid) or "",
            "last_dynamic_is_video": bool(self._last_dynamic_is_video.get(uid)),
            "poll_interval": interval,
            "next_poll_at": datetime.utcfromtimestamp(float(next_time)).isoformat() + "Z",
            "checked_at": datetime.utcnow().isoformat() + "Z",
        }
        update_status(user["id"], status)

    def _cache_last_dynamic(self, uid: str, info: dict):
        self._last_dynamic_text[uid] = info.get("text") or ""
        self._last_dynamic_time[uid] = info.get("time")
        self._last_dynamic_url[uid] = info.get("url")
        self._last_dynamic_title[uid] = info.get("video_title") or ""
        dtype = info.get("type")
        if dtype is None or dtype == "":
            dtype = "video" if info.get("is_video") else "dynamic"
        self._last_dynamic_type[uid] = str(dtype)
        self._last_dynamic_video_url[uid] = info.get("video_url") or ""
        self._last_dynamic_cover[uid] = info.get("cover_url") or ""
        self._last_dynamic_is_video[uid] = bool(info.get("is_video"))

    def _settings_for_binding(self, binding: dict) -> dict:
        profile = binding.get("onebot_profile") or {}
        ws_url = profile.get("ws_url") or binding.get("onebot_ws_url") or self._onebot_defaults.get(
            "ws_url", ""
        )
        access_token = profile.get("access_token") or binding.get(
            "onebot_access_token"
        ) or self._onebot_defaults.get("access_token", "")
        return {
            "onebot_ws_url": ws_url,
            "onebot_access_token": access_token,
            "onebot_target_type": binding.get("onebot_target_type")
            or self._onebot_defaults.get("target_type", "group"),
            "onebot_target_id": binding.get("onebot_target_id")
            or self._onebot_defaults.get("target_id", ""),
        }

    def _onebot_enabled(self, binding: dict) -> bool:
        value = binding.get("enable_onebot")
        if value is None:
            return True
        return bool(value)

    def _notify_live_start(self, binding: dict) -> bool:
        return self._onebot_enabled(binding) and bool(binding.get("notify_live_start", True))

    def _notify_live_hourly(self, binding: dict) -> bool:
        return self._onebot_enabled(binding) and bool(binding.get("notify_live_hourly", True))

    def _notify_live_end(self, binding: dict) -> bool:
        return self._onebot_enabled(binding) and bool(binding.get("notify_live_end", True))

    def _capture_dynamic_screenshot(self, url: str, cookie_header: str = "") -> bytes | None:
        self._logger.info("Dynamic DOM screenshot disabled (Koishi render only).")
        return None

    @staticmethod
    def _cookies_for_playwright(cookie_header: str) -> list[dict]:
        if not cookie_header:
            return []
        cookies = []
        for part in cookie_header.split(";"):
            item = part.strip()
            if not item or "=" not in item:
                continue
            name, value = item.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".bilibili.com",
                    "path": "/",
                }
            )
        return cookies

    @staticmethod
    def _build_cookie_header(credential: dict | None) -> str:
        if not credential:
            return ""
        cookie = credential.get("cookie") or ""
        if cookie:
            return cookie
        parts = []
        mapping = (
            ("SESSDATA", "sessdata"),
            ("bili_jct", "bili_jct"),
            ("buvid3", "buvid3"),
            ("buvid4", "buvid4"),
            ("DedeUserID", "dedeuserid"),
            ("ac_time_value", "ac_time_value"),
        )
        for key, field in mapping:
            val = credential.get(field) or ""
            if val:
                parts.append(f"{key}={val}")
        return "; ".join(parts)

    def _try_expand_dynamic(self, page):
        try:
            fold = page.locator(".bili-dyn-content__fold").first
            if fold and fold.count() > 0:
                fold.click(timeout=800)
                page.wait_for_timeout(500)
        except Exception:
            pass
        try:
            expand = page.locator("text=展开").first
            if expand and expand.count() > 0:
                expand.click(timeout=800)
                page.wait_for_timeout(500)
        except Exception:
            pass

    def _screenshot_dynamic_dom(self, page):
        selectors = [
            ".bili-dyn-content.end",
            ".bili-dyn-content",
            ".bili-dyn-item__main",
            ".bili-dyn-item",
        ]
        for selector in selectors:
            image = self._screenshot_element(page, selector)
            if image:
                return image
        return None

    def _screenshot_element(self, page, selector: str):
        try:
            locator = page.locator(selector).first
            if locator and locator.count() > 0:
                box = locator.bounding_box()
                if box:
                    clip = {
                        "x": box["x"],
                        "y": box["y"],
                        "width": box["width"],
                        "height": box["height"],
                    }
                    return page.screenshot(type="png", clip=clip)
        except Exception:
            return None
        return None

    def _extract_dynamic_outer_html(self, page) -> str:
        selectors = [
            ".bili-dyn-content.end",
            ".bili-dyn-content",
            ".bili-dyn-item__main",
            ".bili-dyn-item",
        ]
        for selector in selectors:
            try:
                html = page.evaluate(
                    """
                    (sel) => {
                      const el = document.querySelector(sel);
                      if (!el) return "";
                      el.querySelectorAll("img").forEach((img) => {
                        if (!img.getAttribute("src")) {
                          const lazy = img.getAttribute("data-src")
                            || img.getAttribute("data-lazy-src")
                            || img.getAttribute("data-original");
                          if (lazy) img.setAttribute("src", lazy);
                        }
                      });
                      return el.outerHTML;
                    }
                    """,
                    selector,
                )
                if html:
                    return html
            except Exception:
                continue
        return ""

    def _wrap_dynamic_html(self, page, outer_html: str) -> str:
        try:
            head_html = page.evaluate(
                """
                () => {
                  const nodes = Array.from(document.head.children).filter((el) => {
                    const tag = el.tagName.toLowerCase();
                    if (tag === "style") return true;
                    if (tag === "link") {
                      const rel = (el.getAttribute("rel") || "").toLowerCase();
                      return rel === "stylesheet";
                    }
                    return false;
                  });
                  return nodes.map((el) => el.outerHTML).join("");
                }
                """
            )
        except Exception:
            head_html = ""
        return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <base href="https://www.bilibili.com/" />
    {head_html}
    <style>
      html, body {{
        margin: 0;
        padding: 0;
        background: #ffffff;
      }}
      #capture-root {{
        padding: 12px;
      }}
    </style>
  </head>
  <body>
    <div id="capture-root">{outer_html}</div>
  </body>
</html>
"""

    @staticmethod
    def _safe_int(value) -> int:
        try:
            return int(value)
        except Exception:
            return 0

    @staticmethod
    def _get_dynamic_id(item: dict) -> str:
        dyn_id = item.get("id_str") or item.get("id")
        if dyn_id is None:
            return ""
        return str(dyn_id)

    @staticmethod
    def _normalize_url(url: str) -> str:
        if not url:
            return ""
        if url.startswith("//"):
            return "https:" + url
        return url

    @staticmethod
    def _name_initial(name: str) -> str:
        if not name:
            return ""
        trimmed = str(name).strip()
        if not trimmed:
            return ""
        return trimmed[:2]

    def _parse_dynamic(self, item: dict) -> dict | None:
        dyn_id = self._get_dynamic_id(item)
        if not dyn_id:
            return None
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        pub_ts = author.get("pub_ts") or author.get("pub_time") or 0
        dynamic = modules.get("module_dynamic") or {}
        text = self._extract_desc_text(dynamic)
        text_html = self._extract_desc_html(dynamic)
        desc_module = self._pick_desc_module(modules)
        if not text:
            text = self._extract_module_desc_text(desc_module)
        if not text_html:
            text_html = self._extract_module_desc_html(desc_module)

        major = dynamic.get("major") or {}
        archive = None
        if "archive" in major:
            archive = major.get("archive")
        elif major.get("type") == "MAJOR_TYPE_ARCHIVE":
            archive = major.get("archive")

        is_video = False
        video_title = ""
        video_url = ""
        cover_url = ""
        if isinstance(archive, dict):
            is_video = True
            video_title = archive.get("title") or ""
            video_url = (
                archive.get("jump_url")
                or archive.get("url")
                or archive.get("bvid")
                or ""
            )
            cover_url = archive.get("cover") or ""
            video_url = self._normalize_url(video_url)
            if video_url and not video_url.startswith("http") and video_url.startswith("BV"):
                video_url = f"https://www.bilibili.com/video/{video_url}"

        avatar, badge = self._extract_author_media(author)
        images, extra = self._extract_dynamic_media(dynamic)
        media_html = self._render_dynamic_media(dynamic, item)
        orig = item.get("orig") or item.get("origin")
        if isinstance(orig, dict):
            orig_dynamic = (orig.get("modules") or {}).get("module_dynamic") or {}
            if isinstance(orig_dynamic, dict):
                orig_images, orig_extra = self._extract_dynamic_media(orig_dynamic)
                if orig_images:
                    images.extend(orig_images)
                if orig_extra and not extra:
                    extra = orig_extra
                if not text:
                    text = self._extract_desc_text(orig_dynamic)
                if not text_html:
                    text_html = self._extract_desc_html(orig_dynamic)
                if not media_html:
                    media_html = self._render_dynamic_media(orig_dynamic, orig)
        url = f"https://t.bilibili.com/{dyn_id}"

        return {
            "id": dyn_id,
            "type": item.get("type") or "",
            "text": text.strip(),
            "time": pub_ts,
            "url": url,
            "is_video": is_video,
            "video_title": video_title.strip(),
            "video_url": video_url.strip(),
            "cover_url": cover_url.strip(),
            "avatar": avatar,
            "images": images,
            "extra": extra,
            "text_html": text_html,
            "media_html": media_html,
        }

    def _ensure_user_profile(self, user: dict, name: str):
        uid = user.get("uid")
        if not uid:
            return
        if uid in self._user_face:
            return
        info = fetch_user_info(uid, user.get("credential"))
        if not info:
            return
        face = info.get("face") or info.get("avatar") or ""
        if face:
            self._user_face[uid] = self._normalize_url(str(face))

    def _extract_author_media(self, author: dict) -> tuple[str, str]:
        avatar = ""
        if isinstance(author, dict):
            avatar = author.get("face") or author.get("avatar") or ""
        return self._normalize_url(str(avatar)) if avatar else "", ""

    def _extract_dynamic_media(self, dynamic: dict) -> tuple[list[str], dict]:
        images = []
        extra = {}
        major = dynamic.get("major") or {}
        if not isinstance(major, dict):
            return images, extra

        images.extend(self._collect_image_urls(major))
        extra = self._extract_extra_card(major)
        return images, extra

    def _collect_image_urls(self, major: dict) -> list[str]:
        images = []
        draw = major.get("draw")
        if isinstance(draw, dict):
            items = draw.get("items") or draw.get("pics") or draw.get("pictures") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = (
                    item.get("src")
                    or item.get("url")
                    or item.get("img_src")
                    or item.get("img")
                )
                if url:
                    images.append(self._normalize_url(str(url)))

        opus = major.get("opus")
        if isinstance(opus, dict):
            pics = opus.get("pics") or opus.get("pictures") or opus.get("images") or []
            if isinstance(pics, list):
                for item in pics:
                    if isinstance(item, dict):
                        url = (
                            item.get("url")
                            or item.get("src")
                            or item.get("img")
                            or item.get("img_src")
                        )
                    else:
                        url = item
                    if url:
                        images.append(self._normalize_url(str(url)))

        article = major.get("article")
        if isinstance(article, dict):
            covers = article.get("covers") or []
            if isinstance(covers, list):
                for cover in covers:
                    if cover:
                        images.append(self._normalize_url(str(cover)))
            cover = article.get("cover")
            if cover:
                images.append(self._normalize_url(str(cover)))

        common = major.get("common")
        if isinstance(common, dict):
            cover = common.get("cover") or ""
            if cover:
                images.append(self._normalize_url(str(cover)))

        ugc = major.get("ugc_season")
        if isinstance(ugc, dict):
            cover = ugc.get("cover") or ""
            if cover:
                images.append(self._normalize_url(str(cover)))

        live = major.get("live_rcmd")
        if isinstance(live, dict):
            cover = live.get("cover") or ""
            if cover:
                images.append(self._normalize_url(str(cover)))

        pgc = major.get("pgc")
        if isinstance(pgc, dict):
            cover = pgc.get("cover") or pgc.get("cover_url") or ""
            if cover:
                images.append(self._normalize_url(str(cover)))

        music = major.get("music")
        if isinstance(music, dict):
            cover = music.get("cover") or music.get("cover_url") or ""
            if cover:
                images.append(self._normalize_url(str(cover)))

        reserve = major.get("reserve")
        if isinstance(reserve, dict):
            cover = reserve.get("cover") or reserve.get("image") or ""
            if cover:
                images.append(self._normalize_url(str(cover)))

        cleaned = []
        seen = set()
        for url in images:
            if not url or url in seen:
                continue
            seen.add(url)
            cleaned.append(url)
        return cleaned

    def _extract_extra_card(self, major: dict) -> dict:
        common = major.get("common")
        if isinstance(common, dict):
            return {
                "title": common.get("title") or common.get("name") or "",
                "desc": common.get("desc") or common.get("summary") or "",
                "url": self._normalize_url(common.get("jump_url") or common.get("url") or ""),
                "cover": self._normalize_url(common.get("cover") or ""),
            }
        article = major.get("article")
        if isinstance(article, dict):
            return {
                "title": article.get("title") or "",
                "desc": article.get("desc") or article.get("summary") or "",
                "url": self._normalize_url(article.get("jump_url") or article.get("url") or ""),
                "cover": self._normalize_url(article.get("cover") or ""),
            }
        archive = major.get("archive")
        if isinstance(archive, dict):
            return {
                "title": archive.get("title") or "",
                "desc": archive.get("desc") or archive.get("desc_text") or "",
                "url": self._normalize_url(archive.get("jump_url") or archive.get("url") or ""),
                "cover": self._normalize_url(archive.get("cover") or ""),
            }
        live = major.get("live_rcmd") or major.get("live")
        if isinstance(live, dict):
            return {
                "title": live.get("title") or live.get("roomname") or "",
                "desc": live.get("desc") or live.get("intro") or "",
                "url": self._normalize_url(live.get("link") or live.get("url") or ""),
                "cover": self._normalize_url(live.get("cover") or live.get("keyframe") or ""),
            }
        reserve = major.get("reserve")
        if isinstance(reserve, dict):
            return {
                "title": reserve.get("title") or "",
                "desc": reserve.get("desc") or reserve.get("desc1") or reserve.get("desc2") or "",
                "url": self._normalize_url(reserve.get("jump_url") or reserve.get("url") or ""),
                "cover": self._normalize_url(reserve.get("cover") or reserve.get("image") or ""),
            }
        opus = major.get("opus")
        if isinstance(opus, dict):
            return {
                "title": opus.get("title") or "",
                "desc": opus.get("summary") or opus.get("content") or "",
                "url": self._normalize_url(opus.get("jump_url") or opus.get("url") or ""),
                "cover": self._normalize_url(opus.get("cover") or ""),
            }
        topic = major.get("topic")
        if isinstance(topic, dict):
            return {
                "title": topic.get("title") or topic.get("name") or "",
                "desc": topic.get("desc") or topic.get("summary") or "",
                "url": self._normalize_url(topic.get("jump_url") or topic.get("url") or ""),
                "cover": self._normalize_url(topic.get("cover") or topic.get("image") or ""),
            }
        medialist = major.get("medialist") or major.get("collection") or major.get("fav")
        if isinstance(medialist, dict):
            return {
                "title": medialist.get("title") or medialist.get("name") or "",
                "desc": medialist.get("desc") or medialist.get("summary") or "",
                "url": self._normalize_url(medialist.get("jump_url") or medialist.get("url") or ""),
                "cover": self._normalize_url(medialist.get("cover") or medialist.get("image") or ""),
            }
        activity = major.get("activity") or major.get("mission") or major.get("courses")
        if isinstance(activity, dict):
            return {
                "title": activity.get("title") or activity.get("name") or "",
                "desc": activity.get("desc") or activity.get("summary") or "",
                "url": self._normalize_url(activity.get("jump_url") or activity.get("url") or ""),
                "cover": self._normalize_url(activity.get("cover") or activity.get("image") or ""),
            }
        return {}

    def _build_media_html(self, images: list[str], extra: dict) -> str:
        parts = []
        if images:
            imgs = []
            seen = set()
            for url in images:
                if url in seen:
                    continue
                seen.add(url)
                if len(imgs) >= 9:
                    break
                safe_url = html_lib.escape(url, quote=True)
                imgs.append(f'<img class="media-img" src="{safe_url}" />')
            parts.append(f'<div class="media-grid">{"".join(imgs)}</div>')

        if isinstance(extra, dict):
            title = self._stringify(extra.get("title"))
            desc = self._stringify(extra.get("desc"))
            url = self._stringify(extra.get("url"))
            cover = self._stringify(extra.get("cover"))
            if images and cover and not (title or desc or url):
                cover = ""
            if images and cover and (title or desc or url):
                cover = ""
            if title or desc or cover or url:
                card = []
                if cover:
                    card.append(
                        f'<img class="media-cover" src="{html_lib.escape(cover, quote=True)}" />'
                    )
                if title:
                    card.append(
                        f'<div class="media-title">{html_lib.escape(title)}</div>'
                    )
                if desc:
                    card.append(
                        f'<div class="media-desc">{html_lib.escape(desc)}</div>'
                    )
                if url:
                    card.append(
                        f'<div class="media-link">{html_lib.escape(url)}</div>'
                    )
                parts.append(f'<div class="media-card">{"".join(card)}</div>')

        return "".join(parts)

    def _render_dynamic_media(self, dynamic: dict, item: dict | None) -> str:
        if not isinstance(dynamic, dict):
            return ""
        major = dynamic.get("major") or {}
        if not isinstance(major, dict):
            major = {}

        parts = []
        major_type = str(major.get("type") or "")

        if "draw" in major or major_type == "MAJOR_TYPE_DRAW":
            images = self._collect_image_urls(major)
            if images:
                parts.append(self._render_image_grid(images))

        if "archive" in major or major_type == "MAJOR_TYPE_ARCHIVE":
            archive = major.get("archive") or {}
            parts.append(self._render_video_card(archive))

        if "article" in major or major_type == "MAJOR_TYPE_ARTICLE":
            article = major.get("article") or {}
            parts.append(self._render_article_card(article))

        if "opus" in major or major_type == "MAJOR_TYPE_OPUS":
            opus = major.get("opus") or {}
            images = self._collect_image_urls({"opus": opus})
            if images:
                parts.append(self._render_image_grid(images))

        if "live_rcmd" in major or major_type == "MAJOR_TYPE_LIVE":
            live = major.get("live_rcmd") or major.get("live") or {}
            parts.append(self._render_live_card(live))

        if "reserve" in major or major_type == "MAJOR_TYPE_RESERVE":
            reserve = major.get("reserve") or major.get("reserve_info") or {}
            parts.append(self._render_reserve_card(reserve))

        if "common" in major or major_type == "MAJOR_TYPE_COMMON":
            common = major.get("common") or {}
            parts.append(self._render_common_card(common))

        if "ugc_season" in major or major_type == "MAJOR_TYPE_UGC_SEASON":
            ugc = major.get("ugc_season") or {}
            parts.append(self._render_common_card(ugc))

        if "pgc" in major or major_type == "MAJOR_TYPE_PGC":
            pgc = major.get("pgc") or {}
            parts.append(self._render_common_card(pgc))

        if "music" in major or major_type == "MAJOR_TYPE_MUSIC":
            music = major.get("music") or {}
            parts.append(self._render_common_card(music))

        if "medialist" in major or major_type == "MAJOR_TYPE_MEDIALIST":
            medialist = major.get("medialist") or {}
            parts.append(self._render_common_card(medialist))

        if "courses" in major or major_type == "MAJOR_TYPE_COURSES_SEASON":
            courses = major.get("courses") or major.get("course") or {}
            parts.append(self._render_common_card(courses))

        if "mission" in major or major_type == "MAJOR_TYPE_MISSION":
            mission = major.get("mission") or {}
            parts.append(self._render_common_card(mission))

        if "topic" in major or major_type == "MAJOR_TYPE_TOPIC":
            topic = major.get("topic") or {}
            parts.append(self._render_common_card(topic))

        if "collection" in major or major_type == "MAJOR_TYPE_COLLECTION":
            collection = major.get("collection") or {}
            parts.append(self._render_common_card(collection))

        if "fav" in major or major_type == "MAJOR_TYPE_FAVORITE":
            fav = major.get("fav") or major.get("favorite") or {}
            parts.append(self._render_common_card(fav))

        if "activity" in major or major_type == "MAJOR_TYPE_ACTIVITY":
            activity = major.get("activity") or {}
            parts.append(self._render_common_card(activity))

        if not parts:
            images = self._collect_image_urls(major)
            if images:
                parts.append(self._render_image_grid(images))

        if not parts:
            generic = self._render_generic_cards(major)
            if generic:
                parts.append(generic)

        if item:
            orig = item.get("orig") or item.get("origin")
            if isinstance(orig, dict):
                orig_dynamic = (orig.get("modules") or {}).get("module_dynamic") or {}
                orig_author = (orig.get("modules") or {}).get("module_author") or {}
                orig_name = orig_author.get("name") or orig_author.get("uname") or ""
                orig_action = orig_author.get("pub_action") or orig_author.get("action") or ""
                orig_face = orig_author.get("face") or orig_author.get("avatar") or ""
                orig_face = self._normalize_url(str(orig_face)) if orig_face else ""
                orig_text = self._extract_desc_html(orig_dynamic)
                orig_media = self._render_dynamic_media(orig_dynamic, None)
                if orig_text or orig_media:
                    header_parts = []
                    if orig_face:
                        header_parts.append(
                            f'<img class="forward-avatar" src="{html_lib.escape(orig_face, quote=True)}" />'
                        )
                    header_parts.append(
                        '<div class="forward-info">'
                        f'<div class="forward-name">{html_lib.escape(orig_name or "原作者")}</div>'
                        f'<div class="forward-action">{html_lib.escape(orig_action or "转发自")}</div>'
                        "</div>"
                    )
                    forward = (
                        f'<div class="forward-card">'
                        f'<div class="forward-header">{"".join(header_parts)}</div>'
                        f'<div class="text">{orig_text}</div>'
                        f'{orig_media}'
                        f"</div>"
                    )
                    parts.append(forward)

        return "".join(part for part in parts if part)

    def _render_generic_cards(self, major: dict) -> str:
        cards = []
        seen = set()
        visited = set()

        def visit(obj):
            obj_id = id(obj)
            if obj_id in visited:
                return
            visited.add(obj_id)
            if isinstance(obj, dict):
                title = obj.get("title") or obj.get("name") or ""
                desc = obj.get("desc") or obj.get("summary") or obj.get("subtitle") or ""
                cover = (
                    obj.get("cover")
                    or obj.get("pic")
                    or obj.get("image")
                    or obj.get("icon")
                    or ""
                )
                url = obj.get("jump_url") or obj.get("url") or obj.get("link") or ""
                key = (title, desc, cover, url)
                if (title or desc) and (cover or url) and key not in seen:
                    seen.add(key)
                    cards.append(
                        self._render_card(
                            title,
                            desc,
                            self._normalize_url(str(cover)) if cover else "",
                            self._normalize_url(str(url)) if url else "",
                        )
                    )
                for value in obj.values():
                    visit(value)
            elif isinstance(obj, list):
                for value in obj:
                    visit(value)

        visit(major)
        return "".join(cards[:2])

    def _render_image_grid(self, images: list[str]) -> str:
        if not images:
            return ""
        imgs = []
        seen = set()
        for url in images:
            if url in seen:
                continue
            seen.add(url)
            if len(imgs) >= 9:
                break
            imgs.append(f'<img class="media-img" src="{html_lib.escape(url, quote=True)}" />')
        return f'<div class="media-grid">{"".join(imgs)}</div>'

    def _render_video_card(self, archive: dict) -> str:
        if not isinstance(archive, dict):
            return ""
        cover = self._normalize_url(archive.get("cover") or "")
        title = archive.get("title") or ""
        desc = archive.get("desc") or archive.get("desc_text") or ""
        stat = archive.get("stat") or {}
        play = stat.get("play") or stat.get("view") or ""
        danmaku = stat.get("danmaku") or ""
        duration = archive.get("duration_text") or archive.get("duration") or ""
        author = archive.get("author") or archive.get("owner") or {}
        author_name = ""
        author_face = ""
        if isinstance(author, dict):
            author_name = author.get("name") or author.get("uname") or ""
            author_face = author.get("face") or author.get("avatar") or ""
            if author_face:
                author_face = self._normalize_url(str(author_face))

        parts = ['<div class="media-card">']
        if author_name or author_face:
            parts.append('<div class="media-author">')
            if author_face:
                parts.append(
                    f'<img class="media-author-avatar" src="{html_lib.escape(author_face, quote=True)}" />'
                )
            parts.append(
                '<div class="media-author-info">'
                f'<div class="media-author-name">{html_lib.escape(author_name or "视频作者")}</div>'
                '<div class="media-author-action">投稿了视频</div>'
                "</div>"
            )
            parts.append("</div>")
        if cover:
            parts.append(
                f'<img class="media-cover" src="{html_lib.escape(cover, quote=True)}" />'
            )
        if title:
            parts.append(f'<div class="media-title">{html_lib.escape(title)}</div>')
        if desc:
            parts.append(f'<div class="media-desc">{html_lib.escape(desc)}</div>')
        if duration:
            parts.append(f'<div class="media-meta">时长：{html_lib.escape(str(duration))}</div>')
        if play or danmaku:
            parts.append(
                f'<div class="media-stats">播放 {html_lib.escape(str(play))} / 弹幕 {html_lib.escape(str(danmaku))}</div>'
            )
        parts.append("</div>")
        return "".join(parts)

    def _render_article_card(self, article: dict) -> str:
        if not isinstance(article, dict):
            return ""
        title = article.get("title") or ""
        desc = article.get("desc") or article.get("summary") or ""
        cover = ""
        covers = article.get("covers") or []
        if isinstance(covers, list) and covers:
            cover = covers[0]
        cover = cover or article.get("cover") or ""
        return self._render_card(title, desc, self._normalize_url(cover), article.get("jump_url") or "")

    def _render_live_card(self, live: dict) -> str:
        if not isinstance(live, dict):
            return ""
        title = live.get("title") or live.get("roomname") or ""
        desc = live.get("desc") or live.get("intro") or ""
        cover = live.get("cover") or live.get("keyframe") or ""
        online = live.get("online") or ""
        return self._render_card(
            title,
            desc,
            self._normalize_url(cover),
            live.get("link") or live.get("url") or "",
            meta=f"人气：{online}" if online else "",
        )

    def _render_reserve_card(self, reserve: dict) -> str:
        if not isinstance(reserve, dict):
            return ""
        title = reserve.get("title") or ""
        desc = reserve.get("desc") or reserve.get("desc1") or reserve.get("desc2") or ""
        time_text = reserve.get("show_time") or reserve.get("pub_time") or ""
        total = reserve.get("reserve_total") or reserve.get("reserve") or ""
        meta = []
        if time_text:
            meta.append(str(time_text))
        if total:
            meta.append(f"预约：{total}")
        return self._render_card(title, desc, "", reserve.get("jump_url") or "", meta=" ".join(meta))

    def _render_common_card(self, common: dict) -> str:
        if not isinstance(common, dict):
            return ""
        title = common.get("title") or common.get("name") or ""
        desc = common.get("desc") or common.get("summary") or common.get("subtitle") or ""
        cover = (
            common.get("cover")
            or common.get("cover_url")
            or common.get("image")
            or common.get("pic")
            or common.get("icon")
            or ""
        )
        return self._render_card(
            title,
            desc,
            self._normalize_url(cover),
            common.get("jump_url") or common.get("url") or "",
        )

    def _render_card(
        self, title: str, desc: str, cover: str, url: str, meta: str = "", stats: str = ""
    ) -> str:
        title = self._stringify(title)
        desc = self._stringify(desc)
        cover = self._stringify(cover)
        url = self._stringify(url)
        meta = self._stringify(meta)
        stats = self._stringify(stats)
        parts = ['<div class="media-card">']
        if cover:
            parts.append(
                f'<img class="media-cover" src="{html_lib.escape(cover, quote=True)}" />'
            )
        if title:
            parts.append(f'<div class="media-title">{html_lib.escape(title)}</div>')
        if desc:
            parts.append(f'<div class="media-desc">{html_lib.escape(desc)}</div>')
        if meta:
            parts.append(f'<div class="media-meta">{html_lib.escape(meta)}</div>')
        if stats:
            parts.append(f'<div class="media-stats">{html_lib.escape(stats)}</div>')
        if url:
            parts.append(f'<div class="media-link">{html_lib.escape(url)}</div>')
        parts.append("</div>")
        return "".join(parts)

    def _extract_desc_html(self, dynamic: dict) -> str:
        desc = dynamic.get("desc")
        if isinstance(desc, dict):
            nodes = desc.get("rich_text_nodes") or []
            if nodes:
                return self._rich_text_nodes_to_html(nodes)
            text = desc.get("text") or ""
            return html_lib.escape(text).replace("\n", "<br>")
        if isinstance(desc, str):
            return html_lib.escape(desc).replace("\n", "<br>")
        if isinstance(desc, list):
            return html_lib.escape("".join(str(x) for x in desc)).replace("\n", "<br>")
        opus = (dynamic.get("major") or {}).get("opus")
        if isinstance(opus, dict):
            summary = opus.get("summary")
            nodes = None
            text = ""
            if isinstance(summary, dict):
                nodes = summary.get("rich_text_nodes") or summary.get("nodes")
                text = summary.get("text") or ""
            elif isinstance(summary, list):
                nodes = summary
            elif isinstance(summary, str):
                text = summary
            if nodes:
                return self._rich_text_nodes_to_html(nodes)
            if text:
                return html_lib.escape(text).replace("\n", "<br>")
            content = opus.get("content") or opus.get("text") or ""
            if content:
                return html_lib.escape(str(content)).replace("\n", "<br>")
        return ""

    @staticmethod
    def _pick_desc_module(modules: dict) -> dict | None:
        if not isinstance(modules, dict):
            return None
        for key in (
            "module_desc",
            "module_comment",
            "module_reply",
            "module_content",
            "module_text",
        ):
            mod = modules.get(key)
            if isinstance(mod, dict):
                return mod
        return None

    def _extract_module_desc_html(self, module: dict | None) -> str:
        if not isinstance(module, dict):
            return ""
        for key in ("desc", "comment", "content", "text", "summary"):
            value = module.get(key)
            if isinstance(value, dict):
                nodes = value.get("rich_text_nodes") or value.get("nodes")
                if isinstance(nodes, list):
                    return self._rich_text_nodes_to_html(nodes)
                text = value.get("text") or value.get("content") or value.get("summary") or ""
                if text:
                    return html_lib.escape(str(text)).replace("\n", "<br>")
            elif isinstance(value, list):
                return self._rich_text_nodes_to_html(value)
            elif isinstance(value, str):
                return html_lib.escape(value).replace("\n", "<br>")
        nodes = module.get("rich_text_nodes") or module.get("nodes")
        if isinstance(nodes, list):
            return self._rich_text_nodes_to_html(nodes)
        return ""

    def _extract_module_desc_text(self, module: dict | None) -> str:
        if not isinstance(module, dict):
            return ""
        for key in ("desc", "comment", "content", "text", "summary"):
            value = module.get(key)
            if isinstance(value, dict):
                text = value.get("text") or value.get("content") or value.get("summary") or ""
                if text:
                    return str(text)
                nodes = value.get("rich_text_nodes") or value.get("nodes")
                if isinstance(nodes, list):
                    return "".join(
                        node.get("text") or "" for node in nodes if isinstance(node, dict)
                    )
            elif isinstance(value, list):
                return "".join(
                    node.get("text") or "" for node in value if isinstance(node, dict)
                )
            elif isinstance(value, str):
                return value
        nodes = module.get("rich_text_nodes") or module.get("nodes")
        if isinstance(nodes, list):
            return "".join(
                node.get("text") or "" for node in nodes if isinstance(node, dict)
            )
        return ""

    def _rich_text_nodes_to_html(self, nodes: list) -> str:
        parts = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            ntype = str(node.get("type") or "")
            text = node.get("text") or ""
            if "EMOJI" in ntype:
                emoji = node.get("emoji") or {}
                url = emoji.get("icon_url") or emoji.get("url") or ""
                if url:
                    parts.append(
                        f'<img src="{html_lib.escape(self._normalize_url(url), quote=True)}" alt="{html_lib.escape(text)}" />'
                    )
                else:
                    parts.append(html_lib.escape(text))
                continue
            if "AT" in ntype:
                name = node.get("user_name") or text or ""
                label = name if name.startswith("@") else f"@{name}" if name else text
                parts.append(html_lib.escape(label))
                continue
            if "TOPIC" in ntype:
                topic = node.get("topic") or text or ""
                label = topic
                if label and not label.startswith("#"):
                    label = f"#{label}#"
                parts.append(html_lib.escape(label))
                continue
            if "LINK" in ntype:
                link = node.get("jump_url") or node.get("link") or ""
                if link:
                    parts.append(
                        f'<a href="{html_lib.escape(self._normalize_url(link), quote=True)}">{html_lib.escape(text or link)}</a>'
                    )
                else:
                    parts.append(html_lib.escape(text))
                continue
            if "WEB" in ntype:
                link = node.get("jump_url") or node.get("url") or ""
                if link:
                    parts.append(
                        f'<a href="{html_lib.escape(self._normalize_url(link), quote=True)}">{html_lib.escape(text or link)}</a>'
                    )
                else:
                    parts.append(html_lib.escape(text))
                continue
            parts.append(html_lib.escape(text))
        return "".join(parts).replace("\n", "<br>")

    @staticmethod
    def _stringify(value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("text", "desc", "summary", "title", "name"):
                val = value.get(key)
                if isinstance(val, str) and val:
                    return val
            nodes = value.get("rich_text_nodes") or value.get("nodes")
            if isinstance(nodes, list):
                parts = []
                for node in nodes:
                    if isinstance(node, dict):
                        parts.append(str(node.get("text") or ""))
                    else:
                        parts.append(str(node))
                return "".join(parts)
            return str(value)
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or ""))
                else:
                    parts.append(str(item))
            return "".join(parts)
        return str(value)

    @staticmethod
    def _extract_desc_text(dynamic: dict) -> str:
        text = ""
        desc = dynamic.get("desc")
        if isinstance(desc, dict):
            text = desc.get("text") or ""
            if not text:
                nodes = desc.get("rich_text_nodes") or []
                if nodes:
                    text = "".join(node.get("text") or "" for node in nodes)
        elif isinstance(desc, str):
            text = desc
        elif isinstance(desc, list):
            text = "".join(str(x) for x in desc)
        if not text:
            opus = (dynamic.get("major") or {}).get("opus")
            if isinstance(opus, dict):
                summary = opus.get("summary")
                if isinstance(summary, dict):
                    text = summary.get("text") or ""
                elif isinstance(summary, str):
                    text = summary
                elif isinstance(summary, list):
                    text = "".join(node.get("text") or "" for node in summary if isinstance(node, dict))
                if not text:
                    content = opus.get("content") or opus.get("text") or ""
                    if content:
                        text = str(content)
        return text
