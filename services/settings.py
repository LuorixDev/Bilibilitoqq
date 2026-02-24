from config import POLL_INTERVAL
from models import AppSetting, db


_GLOBAL_POLL_KEY = "global_poll_interval"


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
