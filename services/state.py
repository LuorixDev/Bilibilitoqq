import json
import logging
from datetime import datetime

from flask import has_app_context

from models import BiliRuntimeStatus, db

_LOGGER = logging.getLogger("bili_state")
_APP = None


def init_state(app):
    global _APP
    _APP = app


def update_status(server_id, status):
    if server_id is None:
        return
    payload = ""
    try:
        payload = json.dumps(status or {}, ensure_ascii=False)
    except Exception:
        payload = "{}"
    try:
        if not has_app_context() and _APP is not None:
            with _APP.app_context():
                return update_status(server_id, status)
        entry = BiliRuntimeStatus.query.get(int(server_id))
        if entry:
            entry.payload = payload
            entry.updated_at = datetime.utcnow()
        else:
            entry = BiliRuntimeStatus(user_id=int(server_id), payload=payload)
            db.session.add(entry)
        db.session.commit()
    except Exception as exc:
        _LOGGER.warning("Status update failed id=%s err=%s", server_id, exc)


def get_status(server_id):
    if server_id is None:
        return None
    try:
        if not has_app_context() and _APP is not None:
            with _APP.app_context():
                return get_status(server_id)
        entry = BiliRuntimeStatus.query.get(int(server_id))
        if not entry or not entry.payload:
            return None
        return json.loads(entry.payload)
    except Exception:
        return None


def all_status():
    results = {}
    try:
        if not has_app_context() and _APP is not None:
            with _APP.app_context():
                return all_status()
        for entry in BiliRuntimeStatus.query.all():
            if not entry.payload:
                continue
            try:
                results[entry.user_id] = json.loads(entry.payload)
            except Exception:
                continue
    except Exception:
        return {}
    return results


def delete_status(server_id):
    if server_id is None:
        return
    try:
        if not has_app_context() and _APP is not None:
            with _APP.app_context():
                return delete_status(server_id)
        entry = BiliRuntimeStatus.query.get(int(server_id))
        if entry:
            db.session.delete(entry)
            db.session.commit()
    except Exception:
        return
