from config import LIVE_HOURLY_INTERVAL, POLL_INTERVAL
from models import AppSetting, db


_GLOBAL_POLL_KEY = "global_poll_interval"
_LIVE_HOURLY_MIN_KEY = "live_hourly_interval_minutes"


def _default_live_minutes() -> int:
    try:
        seconds = int(LIVE_HOURLY_INTERVAL)
    except Exception:
        seconds = 3600
    minutes = max(1, int(round(seconds / 60)))
    minutes = minutes if minutes > 0 else 60
    return max(30, minutes)


def get_global_poll_interval() -> int:
    try:
        setting = AppSetting.query.get(_GLOBAL_POLL_KEY)
    except Exception:
        setting = None
    if not setting or setting.value is None:
        return int(POLL_INTERVAL)
    try:
        value = int(setting.value)
    except Exception:
        value = int(POLL_INTERVAL)
    if value <= 0:
        return int(POLL_INTERVAL)
    return value


def set_global_poll_interval(value: int) -> int:
    try:
        value = int(value)
    except Exception:
        value = int(POLL_INTERVAL)
    if value <= 0:
        value = int(POLL_INTERVAL)
    setting = AppSetting.query.get(_GLOBAL_POLL_KEY)
    if setting:
        setting.value = str(value)
    else:
        setting = AppSetting(key=_GLOBAL_POLL_KEY, value=str(value))
        db.session.add(setting)
    db.session.commit()
    return value


def ensure_global_poll_interval():
    setting = AppSetting.query.get(_GLOBAL_POLL_KEY)
    if setting:
        return
    setting = AppSetting(key=_GLOBAL_POLL_KEY, value=str(int(POLL_INTERVAL)))
    db.session.add(setting)
    db.session.commit()


def get_live_hourly_interval_minutes() -> int:
    default_minutes = _default_live_minutes()
    try:
        setting = AppSetting.query.get(_LIVE_HOURLY_MIN_KEY)
    except Exception:
        setting = None
    if not setting or setting.value is None:
        return default_minutes
    try:
        value = int(setting.value)
    except Exception:
        value = default_minutes
    if value <= 0:
        return default_minutes
    return max(30, value)


def get_live_hourly_interval_seconds() -> int:
    return get_live_hourly_interval_minutes() * 60


def set_live_hourly_interval_minutes(value: int) -> int:
    default_minutes = _default_live_minutes()
    try:
        value = int(value)
    except Exception:
        value = default_minutes
    if value <= 0:
        value = default_minutes
    value = max(30, value)
    setting = AppSetting.query.get(_LIVE_HOURLY_MIN_KEY)
    if setting:
        setting.value = str(value)
    else:
        setting = AppSetting(key=_LIVE_HOURLY_MIN_KEY, value=str(value))
        db.session.add(setting)
    db.session.commit()
    return value


def ensure_live_hourly_interval():
    setting = AppSetting.query.get(_LIVE_HOURLY_MIN_KEY)
    if setting:
        return
    setting = AppSetting(key=_LIVE_HOURLY_MIN_KEY, value=str(_default_live_minutes()))
    db.session.add(setting)
    db.session.commit()
