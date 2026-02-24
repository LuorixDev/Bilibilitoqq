import logging
import re
from typing import Any
from urllib.request import Request, urlopen

from bilibili_api import Credential, request_settings, select_client, sync
from bilibili_api import user as bili_user

from config import (
    BILIBILI_AC_TIME_VALUE,
    BILIBILI_BILI_JCT,
    BILIBILI_BUVID3,
    BILIBILI_BUVID4,
    BILIBILI_COOKIE,
    BILIBILI_DEDEUSERID,
    BILIBILI_HTTP_CLIENT,
    BILIBILI_IMPERSONATE,
    BILIBILI_PROXY,
    BILIBILI_SESSDATA,
    BILIBILI_USER_AGENT,
    HTTP_TIMEOUT,
)

_LOGGER = logging.getLogger("bili_api")
_INITIALIZED = False


def _init_client():
    global _INITIALIZED
    if _INITIALIZED:
        return
    if BILIBILI_HTTP_CLIENT:
        try:
            select_client(BILIBILI_HTTP_CLIENT)
        except Exception as exc:
            _LOGGER.warning("Bili API select client failed: %s", exc)
    try:
        if BILIBILI_PROXY:
            request_settings.set_proxy(BILIBILI_PROXY)
        if HTTP_TIMEOUT:
            request_settings.set_timeout(HTTP_TIMEOUT)
        if BILIBILI_IMPERSONATE:
            request_settings.set("impersonate", BILIBILI_IMPERSONATE)
    except Exception as exc:
        _LOGGER.warning("Bili API request settings failed: %s", exc)
    _INITIALIZED = True


def _cookie_value(cookie: str, key: str) -> str:
    if not cookie:
        return ""
    match = re.search(rf"(?:^|;)\s*{re.escape(key)}=([^;]*)", cookie)
    if not match:
        return ""
    return match.group(1)


def _resolve_value(primary: str, cookie: str, key: str, fallback: str) -> str:
    return primary or _cookie_value(cookie, key) or fallback


def _build_credential(data: dict | None = None) -> Credential:
    data = data or {}
    cookie = data.get("cookie") or ""
    sessdata = _resolve_value(
        data.get("sessdata") or "", cookie, "SESSDATA", BILIBILI_SESSDATA
    )
    bili_jct = _resolve_value(
        data.get("bili_jct") or "", cookie, "bili_jct", BILIBILI_BILI_JCT
    )
    buvid3 = _resolve_value(
        data.get("buvid3") or "", cookie, "buvid3", BILIBILI_BUVID3
    )
    buvid4 = _resolve_value(
        data.get("buvid4") or "", cookie, "buvid4", BILIBILI_BUVID4
    )
    dedeuserid = _resolve_value(
        data.get("dedeuserid") or "", cookie, "DedeUserID", BILIBILI_DEDEUSERID
    )
    ac_time_value = _resolve_value(
        data.get("ac_time_value") or "", cookie, "ac_time_value", BILIBILI_AC_TIME_VALUE
    )
    return Credential(
        sessdata=sessdata or None,
        bili_jct=bili_jct or None,
        buvid3=buvid3 or None,
        buvid4=buvid4 or None,
        dedeuserid=dedeuserid or None,
        ac_time_value=ac_time_value or None,
    )


def _headers() -> dict:
    headers = {
        "User-Agent": BILIBILI_USER_AGENT,
        "Referer": "https://space.bilibili.com/",
        "Origin": "https://space.bilibili.com",
    }
    if BILIBILI_COOKIE:
        headers["Cookie"] = BILIBILI_COOKIE
    return headers


def fetch_user_info(uid: str, credential_data: dict | None = None) -> dict | None:
    _init_client()
    try:
        _LOGGER.debug("Bili API user info uid=%s", uid, extra={"uid": uid})
        user = bili_user.User(int(uid), credential=_build_credential(credential_data))
        data = sync(user.get_user_info())
        return data
    except Exception as exc:
        _LOGGER.warning("Bili API user info failed uid=%s err=%s", uid, exc)
        return None


def fetch_live_info(uid: str, credential_data: dict | None = None) -> dict | None:
    _init_client()
    try:
        _LOGGER.debug("Bili API live info uid=%s", uid, extra={"uid": uid})
        user = bili_user.User(int(uid), credential=_build_credential(credential_data))
        data = sync(user.get_live_info())
        return data
    except Exception as exc:
        _LOGGER.warning("Bili API live info failed uid=%s err=%s", uid, exc)
        return None


def fetch_dynamic_list(
    uid: str, offset: str | None = None, credential_data: dict | None = None
) -> list[dict[str, Any]] | None:
    _init_client()
    try:
        _LOGGER.debug(
            "Bili API dynamics uid=%s offset=%s", uid, offset or "-", extra={"uid": uid}
        )
        user = bili_user.User(int(uid), credential=_build_credential(credential_data))
        if offset:
            data = sync(user.get_dynamics_new(offset=offset))
        else:
            data = sync(user.get_dynamics_new())
        if not isinstance(data, dict):
            return None
        items = data.get("items")
        if items is None:
            items = (data.get("data") or {}).get("items")
        if not isinstance(items, list):
            return None
        filtered = [item for item in items if not _is_pinned_dynamic(item)]
        _LOGGER.debug(
            "Bili API dynamics uid=%s total=%s filtered=%s",
            uid,
            len(items),
            len(filtered),
            extra={"uid": uid},
        )
        return filtered
    except Exception as exc:
        _LOGGER.warning("Bili API dynamics failed uid=%s err=%s", uid, exc)
        return None


def _is_pinned_dynamic(item: dict | None) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("is_top") or item.get("is_pinned") or item.get("is_fixed"):
        return True
    modules = item.get("modules")
    if not isinstance(modules, dict):
        return False
    for key in ("module_tag", "module_top", "module_anchor", "module_author"):
        mod = modules.get(key)
        if isinstance(mod, dict):
            if mod.get("is_top") or mod.get("is_pinned") or mod.get("is_fixed"):
                return True
            text = mod.get("text") or mod.get("title") or mod.get("tag_text") or ""
            label = mod.get("label") or mod.get("desc") or ""
            if "置顶" in str(text) or "置顶" in str(label):
                return True
        elif isinstance(mod, list):
            for entry in mod:
                if not isinstance(entry, dict):
                    continue
                text = entry.get("text") or entry.get("title") or ""
                if "置顶" in str(text):
                    return True
    return False


def fetch_latest_video(uid: str, credential_data: dict | None = None) -> dict | None:
    _init_client()
    try:
        user = bili_user.User(int(uid), credential=_build_credential(credential_data))
        data = sync(user.get_videos(pn=1, ps=1, order="pubdate"))
        vlist = (((data.get("list") or {}).get("vlist")) or []) if isinstance(data, dict) else []
        if not vlist:
            return None
        return vlist[0]
    except Exception as exc:
        _LOGGER.warning("Bili API latest video failed uid=%s err=%s", uid, exc)
        return None


def download_image(url: str) -> bytes | None:
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    try:
        req = Request(url, headers=_headers())
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except Exception as exc:
        _LOGGER.warning("Bili image fetch failed %s err=%s", url, exc)
        return None
