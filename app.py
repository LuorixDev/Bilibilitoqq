import base64
import html as html_lib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    ADMIN_PASSWORD,
    ADMIN_PASSWORD_HASH,
    ADMIN_USERNAME,
    DATABASE_URL,
    LOGS_DATABASE_URL,
    LOG_BACKUP_COUNT,
    LOG_FILE,
    LOG_MAX_BYTES,
    POLL_INTERVAL,
    SECRET_KEY,
    STATUS_DATABASE_URL,
    TEMPLATES_DATABASE_URL,
)
from models import BiliBinding, BiliLogEntry, BiliUser, OneBotProfile, db
from services.bili_api import (
    download_image,
    fetch_dynamic_list,
    fetch_live_info,
    fetch_live_room_cover,
    fetch_user_info,
)
from services.html_screenshot import render_html_to_image
from services.message_templates import DEFAULT_TEMPLATES, PLACEHOLDER_HINT
from services.screenshot_templates import (
    DEFAULT_HTML_TEMPLATES,
    HTML_TEMPLATE_VARS,
    render_html_template,
)
from services.monitor import BiliMonitor
from services.onebot_manager import OneBotManager
from services.screenshot_store import (
    delete_screenshot_templates,
    ensure_screenshot_templates,
    get_screenshot_template_value,
    save_screenshot_templates,
)
from services.settings import (
    ensure_global_poll_interval,
    ensure_live_hourly_interval,
    get_global_poll_interval,
    get_live_hourly_interval_minutes,
    set_global_poll_interval,
    set_live_hourly_interval_minutes,
)
from services.state import delete_status, get_status, init_state
from services.time_utils import format_duration

_SPECIAL_PATTERN = re.compile(r"(\{SHOTPICTURE\}|\[atALL\])")

@dataclass
class AdminUser(UserMixin):
    username: str

    def get_id(self):
        return "admin"

    @property
    def is_admin(self) -> bool:
        return True


@dataclass
class UpUser(UserMixin):
    user_id: int
    uid: str
    name: str
    login_username: str

    def get_id(self):
        return f"user:{self.user_id}"

    @property
    def is_admin(self) -> bool:
        return False


ADMIN_USER = AdminUser(username=ADMIN_USERNAME)

if ADMIN_PASSWORD_HASH:
    _ADMIN_HASH = ADMIN_PASSWORD_HASH
else:
    _ADMIN_HASH = generate_password_hash(ADMIN_PASSWORD)

class _UidExtractFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "uid") or record.uid in (None, ""):
            msg = record.getMessage()
            match = re.search(r"uid=([0-9]+)", msg)
            if match:
                record.uid = match.group(1)
            else:
                record.uid = ""
        return True


class _JsonLogFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "uid": getattr(record, "uid", ""),
            "module": record.module,
            "line": record.lineno,
            "thread": record.threadName,
        }
        return json.dumps(payload, ensure_ascii=False)


class _DbLogHandler(logging.Handler):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.setLevel(logging.DEBUG)
        self.addFilter(_UidExtractFilter())

    def emit(self, record):
        try:
            payload = {
                "time": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "uid": getattr(record, "uid", ""),
                "module": record.module,
                "line": record.lineno,
                "thread": record.threadName,
            }
            data = json.dumps(payload, ensure_ascii=False)
            with self.engine.begin() as conn:
                conn.execute(
                    BiliLogEntry.__table__.insert().values(
                        time=payload["time"],
                        level=payload["level"],
                        logger=payload["logger"],
                        message=payload["message"],
                        uid=payload["uid"],
                        module=payload["module"],
                        line=payload["line"],
                        thread=payload["thread"],
                        payload=data,
                    )
                )
        except Exception:
            return


def _setup_file_logging():
    if not LOG_FILE:
        return
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler) and getattr(
            handler, "baseFilename", None
        ) == os.path.abspath(LOG_FILE):
            return

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_JsonLogFormatter())
    file_handler.addFilter(_UidExtractFilter())
    root.addHandler(file_handler)

    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setLevel(logging.INFO)
    root.setLevel(logging.DEBUG)

    logging.getLogger("bilibili_api").setLevel(logging.DEBUG)
    logging.getLogger("bili_api").setLevel(logging.DEBUG)
    logging.getLogger("bili_monitor").setLevel(logging.DEBUG)


def _setup_db_logging(app: Flask):
    if not LOGS_DATABASE_URL:
        return
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, _DbLogHandler):
            return
    try:
        engine = db.get_engine(app, bind="logs")
    except Exception:
        return
    db_handler = _DbLogHandler(engine)
    root.addHandler(db_handler)


login_manager = LoginManager()
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    if user_id == "admin":
        return ADMIN_USER
    if user_id and user_id.startswith("user:"):
        try:
            uid = int(user_id.split(":", 1)[1])
        except ValueError:
            return None
        user = BiliUser.query.get(uid)
        if not user or not user.enabled:
            return None
        return UpUser(
            user_id=user.id,
            uid=user.uid,
            name=user.name,
            login_username=user.login_username or user.uid,
        )
    return None


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not getattr(current_user, "is_admin", False):
            flash("无权限访问该页面", "error")
            return redirect(url_for("user_bindings"))
        return fn(*args, **kwargs)

    return wrapper


def user_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if getattr(current_user, "is_admin", False):
            return redirect(url_for("admin"))
        return fn(*args, **kwargs)

    return wrapper


def create_app():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _setup_file_logging()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
    app.config["SQLALCHEMY_BINDS"] = {
        "logs": LOGS_DATABASE_URL,
        "status": STATUS_DATABASE_URL,
        "templates": TEMPLATES_DATABASE_URL,
    }
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    login_manager.init_app(app)
    init_state(app)

    with app.app_context():
        db.create_all()
        for bind_key in ("logs", "status", "templates"):
            try:
                db.create_all(bind=bind_key)
            except Exception:
                continue
        _ensure_user_columns()
        _ensure_onebot_profile_columns()
        _ensure_binding_columns()
        _seed_bindings()
        _ensure_screenshot_template_records()
        ensure_global_poll_interval()
        ensure_live_hourly_interval()
        _setup_db_logging(app)

    onebot_defaults = {}

    onebot = OneBotManager(onebot_defaults)
    monitor = BiliMonitor(app, onebot, onebot_defaults)
    app.extensions["bili_monitor"] = monitor

    def _reset_monitor_state(uid: str | int | None):
        if uid is None:
            return
        monitor_obj = app.extensions.get("bili_monitor")
        if monitor_obj:
            try:
                monitor_obj.reset_user_state(uid)
            except Exception:
                return

    def _start_background():
        onebot.start()
        monitor.start()

    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _start_background()

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            if username == ADMIN_USERNAME and check_password_hash(_ADMIN_HASH, password):
                login_user(ADMIN_USER)
                return redirect(url_for("admin"))

            user = _find_user_by_login(username)
            if user and user.enabled and user.password_hash:
                if check_password_hash(user.password_hash, password):
                    login_user(
                        UpUser(
                            user_id=user.id,
                            uid=user.uid,
                            name=user.name,
                            login_username=user.login_username or user.uid,
                        )
                    )
                    return redirect(url_for("user_bindings"))
            elif user and not user.password_hash:
                flash("该账号未设置密码，请联系管理员", "error")
                return render_template("login.html")

            flash("用户名或密码错误", "error")

        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("index"))

    @app.route("/refresh", methods=["POST"])
    @login_required
    def refresh_now():
        monitor = app.extensions.get("bili_monitor")
        if monitor:
            threading.Thread(target=monitor._poll_once, kwargs={"force": True}, daemon=True).start()
        flash("已触发检查更新", "success")
        return redirect(request.referrer or url_for("index"))

    @app.route("/admin")
    @admin_required
    def admin():
        users = BiliUser.query.order_by(BiliUser.id.desc()).all()
        return render_template(
            "admin.html",
            users=users,
            global_poll_interval=get_global_poll_interval(),
            live_hourly_interval=get_live_hourly_interval_minutes(),
        )

    @app.route("/admin/settings", methods=["POST"])
    @admin_required
    def admin_settings():
        poll_value = request.form.get("global_poll_interval", "").strip()
        live_value = request.form.get("live_hourly_interval", "").strip()

        if not poll_value:
            flash("请输入全局检测间隔（秒）", "error")
            return redirect(url_for("admin"))

        try:
            poll_interval = int(poll_value)
        except ValueError:
            flash("全局检测间隔必须是数字", "error")
            return redirect(url_for("admin"))
        if poll_interval <= 0:
            flash("全局检测间隔必须大于 0", "error")
            return redirect(url_for("admin"))

        set_global_poll_interval(poll_interval)
        if live_value:
            try:
                live_minutes = int(live_value)
            except ValueError:
                flash("直播播报间隔必须是数字", "error")
                return redirect(url_for("admin"))
            if live_minutes <= 0:
                flash("直播播报间隔必须大于 0", "error")
                return redirect(url_for("admin"))
            set_live_hourly_interval_minutes(live_minutes)
        flash("设置已更新", "success")
        return redirect(url_for("admin"))

    @app.route("/admin/add", methods=["POST"])
    @admin_required
    def admin_add():
        uid = request.form.get("uid", "").strip()
        name = request.form.get("name", "").strip()
        login_username = request.form.get("login_username", "").strip()
        password = request.form.get("password", "").strip()
        poll_interval_raw = request.form.get("poll_interval", "").strip()

        if not uid:
            flash("UID 不能为空", "error")
            return redirect(url_for("admin"))

        if BiliUser.query.filter_by(uid=uid).first():
            flash("该 UID 已存在", "error")
            return redirect(url_for("admin"))

        if not name:
            info = fetch_user_info(uid)
            if info:
                name = info.get("name") or info.get("uname") or ""

        if not login_username:
            login_username = uid
        else:
            existing_login = BiliUser.query.filter_by(login_username=login_username).first()
            if existing_login:
                flash("登录名已被占用", "error")
                return redirect(url_for("admin"))

        password_hash = generate_password_hash(password) if password else ""

        poll_interval = 0
        if poll_interval_raw:
            try:
                poll_interval = int(poll_interval_raw)
            except ValueError:
                poll_interval = 0
        if poll_interval < 0:
            poll_interval = 0

        user = BiliUser(
            uid=uid,
            name=name or f"UID {uid}",
            enabled=True,
            login_username=login_username,
            password_hash=password_hash,
            poll_interval=poll_interval,
        )
        db.session.add(user)
        db.session.commit()

        flash("UP 主已添加", "success")
        return redirect(url_for("admin"))

    @app.route("/admin/delete/<int:user_id>", methods=["POST"])
    @admin_required
    def admin_delete(user_id):
        user = BiliUser.query.get_or_404(user_id)
        uid = user.uid
        binding_ids = [binding.id for binding in user.bindings]
        for binding_id in binding_ids:
            delete_screenshot_templates(binding_id)
        delete_status(user.id)
        db.session.delete(user)
        db.session.commit()
        _reset_monitor_state(uid)
        flash("UP 主已删除", "success")
        return redirect(url_for("admin"))

    @app.route("/admin/edit/<int:user_id>", methods=["GET", "POST"])
    @admin_required
    def admin_edit(user_id):
        user = BiliUser.query.get_or_404(user_id)
        if request.method == "POST":
            action = request.form.get("action", "save").strip()
            uid = request.form.get("uid", "").strip()
            name = request.form.get("name", "").strip()
            enabled = bool(request.form.get("enabled"))
            login_username = request.form.get("login_username", "").strip()
            password = request.form.get("password", "").strip()
            poll_interval_raw = request.form.get("poll_interval", "").strip()
            cookie_input = request.form.get("cookie", "").strip()
            sessdata = request.form.get("sessdata", "").strip()
            bili_jct = request.form.get("bili_jct", "").strip()
            buvid3 = request.form.get("buvid3", "").strip()
            buvid4 = request.form.get("buvid4", "").strip()
            dedeuserid = request.form.get("dedeuserid", "").strip()
            ac_time_value = request.form.get("ac_time_value", "").strip()

            if not uid:
                flash("UID 不能为空", "error")
                return redirect(url_for("admin_edit", user_id=user_id))

            if action == "test_credential":
                payload = _build_credential_payload_from_form(
                    user,
                    cookie_input,
                    sessdata,
                    bili_jct,
                    buvid3,
                    buvid4,
                    dedeuserid,
                    ac_time_value,
                )
                ok, message = _test_credential_payload(uid, payload)
                if ok is True:
                    flash("凭据验证通过", "success")
                elif ok is False:
                    flash(f"凭据验证失败：{message}", "error")
                else:
                    flash("未填写凭据，无法测试", "error")
                view_user = _build_view_user(
                    user,
                    payload,
                    uid=uid,
                    name=name or f"UID {uid}",
                    login_username=login_username or uid,
                    enabled=enabled,
                    poll_interval=user.poll_interval,
                )
                return render_template(
                    "edit_user.html",
                    user=view_user,
                    cookie_value=cookie_input,
                    poll_interval_default=get_global_poll_interval(),
                )

            exists = BiliUser.query.filter(BiliUser.uid == uid, BiliUser.id != user_id).first()
            if exists:
                flash("该 UID 已存在", "error")
                return redirect(url_for("admin_edit", user_id=user_id))

            if login_username:
                dup_login = BiliUser.query.filter(
                    BiliUser.login_username == login_username, BiliUser.id != user_id
                ).first()
                if dup_login:
                    flash("该登录名已被占用", "error")
                    return redirect(url_for("admin_edit", user_id=user_id))
            else:
                login_username = uid

            user.uid = uid
            user.name = name or f"UID {uid}"
            user.enabled = enabled
            user.login_username = login_username
            if password:
                user.password_hash = generate_password_hash(password)

            poll_interval = 0
            if poll_interval_raw:
                try:
                    poll_interval = int(poll_interval_raw)
                except ValueError:
                    poll_interval = user.poll_interval or 0
            if poll_interval < 0:
                poll_interval = 0
            user.poll_interval = poll_interval

            payload = _build_credential_payload_from_form(
                user,
                cookie_input,
                sessdata,
                bili_jct,
                buvid3,
                buvid4,
                dedeuserid,
                ac_time_value,
            )
            if cookie_input:
                user.cookie = cookie_input
            user.sessdata = payload.get("sessdata", "")
            user.bili_jct = payload.get("bili_jct", "")
            user.buvid3 = payload.get("buvid3", "")
            user.buvid4 = payload.get("buvid4", "")
            user.dedeuserid = payload.get("dedeuserid", "")
            user.ac_time_value = payload.get("ac_time_value", "")

            if action == "clear_credential":
                _clear_user_credential(user)
            db.session.commit()

            if action == "clear_credential":
                flash("UP 主凭据已清空", "success")
                return redirect(url_for("admin_edit", user_id=user_id))

            ok, message = _test_user_credential(user)
            if ok is True:
                flash("UP 主已更新，凭据验证通过", "success")
            elif ok is False:
                flash(f"UP 主已更新，但凭据验证失败：{message}", "error")
            else:
                flash("UP 主已更新", "success")
            return redirect(url_for("admin"))

        return render_template(
            "edit_user.html",
            user=user,
            poll_interval_default=get_global_poll_interval(),
        )

    @app.route("/admin/onebot", methods=["GET", "POST"])
    @admin_required
    def admin_onebot():
        if request.method == "POST":
            name = request.form.get("name", "").strip() or "默认"
            ws_url = request.form.get("ws_url", "").strip()
            access_token = request.form.get("access_token", "").strip()

            if not ws_url:
                flash("WS 地址不能为空", "error")
                return redirect(url_for("admin_onebot"))

            profile = OneBotProfile(name=name, ws_url=ws_url, access_token=access_token)
            db.session.add(profile)
            db.session.commit()
            flash("OneBot 配置已添加", "success")
            return redirect(url_for("admin_onebot"))

        profiles = OneBotProfile.query.order_by(OneBotProfile.id.desc()).all()
        return render_template("onebot.html", profiles=profiles)

    @app.route("/admin/onebot/edit/<int:profile_id>", methods=["GET", "POST"])
    @admin_required
    def admin_onebot_edit(profile_id):
        profile = OneBotProfile.query.get_or_404(profile_id)
        if request.method == "POST":
            name = request.form.get("name", "").strip() or "默认"
            ws_url = request.form.get("ws_url", "").strip()
            access_token = request.form.get("access_token", "").strip()

            if not ws_url:
                flash("WS 地址不能为空", "error")
                return redirect(url_for("admin_onebot_edit", profile_id=profile_id))

            profile.name = name
            profile.ws_url = ws_url
            profile.access_token = access_token
            db.session.commit()
            flash("OneBot 配置已更新", "success")
            return redirect(url_for("admin_onebot"))

        return render_template("edit_onebot.html", profile=profile)

    @app.route("/admin/onebot/delete/<int:profile_id>", methods=["POST"])
    @admin_required
    def admin_onebot_delete(profile_id):
        profile = OneBotProfile.query.get_or_404(profile_id)
        db.session.delete(profile)
        db.session.commit()
        flash("OneBot 配置已删除", "success")
        return redirect(url_for("admin_onebot"))

    @app.route("/admin/bindings/<int:user_id>")
    @admin_required
    def admin_bindings(user_id):
        user = BiliUser.query.get_or_404(user_id)
        bindings = BiliBinding.query.filter_by(user_id=user_id).order_by(BiliBinding.id.desc()).all()
        return render_template(
            "bindings.html",
            user=user,
            bindings=bindings,
            is_admin=True,
            new_url=url_for("admin_binding_new", user_id=user.id),
            back_url=url_for("admin"),
        )

    @app.route("/admin/bindings/<int:user_id>/new", methods=["GET", "POST"])
    @admin_required
    def admin_binding_new(user_id):
        user = BiliUser.query.get_or_404(user_id)
        profiles = OneBotProfile.query.order_by(OneBotProfile.id.desc()).all()
        if request.method == "POST":
            binding = _build_binding_from_form(user.id)
            db.session.add(binding)
            db.session.commit()
            template_dynamic, template_live = _read_screenshot_templates_from_form()
            save_screenshot_templates(binding.id, template_dynamic, template_live)
            flash("绑定已添加", "success")
            return redirect(url_for("admin_bindings", user_id=user.id))

        return render_template(
            "add_binding.html",
            user=user,
            profiles=profiles,
            defaults=DEFAULT_TEMPLATES,
            placeholder_hint=PLACEHOLDER_HINT,
            html_defaults=DEFAULT_HTML_TEMPLATES,
            html_vars=HTML_TEMPLATE_VARS,
            live_hourly_interval_default=max(30, get_live_hourly_interval_minutes()),
            submit_label="新增绑定",
            back_url=url_for("admin_bindings", user_id=user.id),
        )

    @app.route("/admin/bindings/edit/<int:binding_id>", methods=["GET", "POST"])
    @admin_required
    def admin_binding_edit(binding_id):
        binding = BiliBinding.query.get_or_404(binding_id)
        profiles = OneBotProfile.query.order_by(OneBotProfile.id.desc()).all()
        dynamics = _get_recent_dynamics(binding.user)
        if request.method == "POST":
            _update_binding_from_form(binding)
            db.session.commit()
            template_dynamic, template_live = _read_screenshot_templates_from_form()
            save_screenshot_templates(binding.id, template_dynamic, template_live)
            flash("绑定已更新", "success")
            return redirect(url_for("admin_bindings", user_id=binding.user_id))

        binding.screenshot_template_dynamic = get_screenshot_template_value(binding.id, "dynamic")
        binding.screenshot_template_live = get_screenshot_template_value(binding.id, "live")
        return render_template(
            "edit_binding.html",
            binding=binding,
            profiles=profiles,
            defaults=DEFAULT_TEMPLATES,
            placeholder_hint=PLACEHOLDER_HINT,
            html_defaults=DEFAULT_HTML_TEMPLATES,
            html_vars=HTML_TEMPLATE_VARS,
            dynamics=dynamics,
            live_hourly_interval_default=max(30, get_live_hourly_interval_minutes()),
            back_url=url_for("admin_bindings", user_id=binding.user_id),
        )

    @app.route("/admin/bindings/delete/<int:binding_id>", methods=["POST"])
    @admin_required
    def admin_binding_delete(binding_id):
        binding = BiliBinding.query.get_or_404(binding_id)
        user_id = binding.user_id
        uid = binding.user.uid if binding.user else None
        delete_screenshot_templates(binding.id)
        db.session.delete(binding)
        db.session.commit()
        if uid:
            remaining = BiliBinding.query.filter_by(user_id=user_id).count()
            if remaining == 0:
                delete_status(user_id)
                _reset_monitor_state(uid)
        flash("绑定已删除", "success")
        return redirect(url_for("admin_bindings", user_id=user_id))

    @app.route("/me/bindings")
    @user_required
    def user_bindings():
        user = BiliUser.query.get_or_404(current_user.user_id)
        bindings = BiliBinding.query.filter_by(user_id=user.id).order_by(BiliBinding.id.desc()).all()
        return render_template(
            "bindings.html",
            user=user,
            bindings=bindings,
            is_admin=False,
            new_url=url_for("user_binding_new"),
            back_url=url_for("user_bindings"),
        )

    @app.route("/me/bindings/new", methods=["GET", "POST"])
    @user_required
    def user_binding_new():
        user = BiliUser.query.get_or_404(current_user.user_id)
        profiles = OneBotProfile.query.order_by(OneBotProfile.id.desc()).all()
        if request.method == "POST":
            binding = _build_binding_from_form(user.id)
            db.session.add(binding)
            db.session.commit()
            template_dynamic, template_live = _read_screenshot_templates_from_form()
            save_screenshot_templates(binding.id, template_dynamic, template_live)
            flash("绑定已添加", "success")
            return redirect(url_for("user_bindings"))

        return render_template(
            "add_binding.html",
            user=user,
            profiles=profiles,
            defaults=DEFAULT_TEMPLATES,
            placeholder_hint=PLACEHOLDER_HINT,
            html_defaults=DEFAULT_HTML_TEMPLATES,
            html_vars=HTML_TEMPLATE_VARS,
            live_hourly_interval_default=max(30, get_live_hourly_interval_minutes()),
            submit_label="新增绑定",
            back_url=url_for("user_bindings"),
        )

    @app.route("/me/bindings/edit/<int:binding_id>", methods=["GET", "POST"])
    @user_required
    def user_binding_edit(binding_id):
        binding = BiliBinding.query.get_or_404(binding_id)
        if binding.user_id != current_user.user_id:
            flash("无权限编辑该绑定", "error")
            return redirect(url_for("user_bindings"))
        profiles = OneBotProfile.query.order_by(OneBotProfile.id.desc()).all()
        dynamics = _get_recent_dynamics(binding.user)
        if request.method == "POST":
            _update_binding_from_form(binding)
            db.session.commit()
            template_dynamic, template_live = _read_screenshot_templates_from_form()
            save_screenshot_templates(binding.id, template_dynamic, template_live)
            flash("绑定已更新", "success")
            return redirect(url_for("user_bindings"))

        binding.screenshot_template_dynamic = get_screenshot_template_value(binding.id, "dynamic")
        binding.screenshot_template_live = get_screenshot_template_value(binding.id, "live")
        return render_template(
            "edit_binding.html",
            binding=binding,
            profiles=profiles,
            defaults=DEFAULT_TEMPLATES,
            placeholder_hint=PLACEHOLDER_HINT,
            html_defaults=DEFAULT_HTML_TEMPLATES,
            html_vars=HTML_TEMPLATE_VARS,
            dynamics=dynamics,
            live_hourly_interval_default=max(30, get_live_hourly_interval_minutes()),
            back_url=url_for("user_bindings"),
        )

    @app.route("/me/bindings/delete/<int:binding_id>", methods=["POST"])
    @user_required
    def user_binding_delete(binding_id):
        binding = BiliBinding.query.get_or_404(binding_id)
        if binding.user_id != current_user.user_id:
            flash("无权限删除该绑定", "error")
            return redirect(url_for("user_bindings"))
        uid = binding.user.uid if binding.user else None
        delete_screenshot_templates(binding.id)
        db.session.delete(binding)
        db.session.commit()
        if uid:
            remaining = BiliBinding.query.filter_by(user_id=binding.user_id).count()
            if remaining == 0:
                delete_status(binding.user_id)
                _reset_monitor_state(uid)
        flash("绑定已删除", "success")
        return redirect(url_for("user_bindings"))

    @app.route("/me/password", methods=["GET", "POST"])
    @user_required
    def user_password():
        user = BiliUser.query.get_or_404(current_user.user_id)
        if request.method == "POST":
            current_pwd = request.form.get("current_password", "")
            new_pwd = request.form.get("new_password", "")
            if user.password_hash:
                if not check_password_hash(user.password_hash, current_pwd):
                    flash("当前密码不正确", "error")
                    return redirect(url_for("user_password"))
            if not new_pwd:
                flash("新密码不能为空", "error")
                return redirect(url_for("user_password"))
            user.password_hash = generate_password_hash(new_pwd)
            db.session.commit()
            flash("密码已更新", "success")
            return redirect(url_for("user_bindings"))
        return render_template("change_password.html", user=user)

    @app.route("/me/credential", methods=["GET", "POST"])
    @user_required
    def user_credential():
        user = BiliUser.query.get_or_404(current_user.user_id)
        if request.method == "POST":
            action = request.form.get("action", "save").strip()
            cookie_input = request.form.get("cookie", "").strip()
            sessdata = request.form.get("sessdata", "").strip()
            bili_jct = request.form.get("bili_jct", "").strip()
            buvid3 = request.form.get("buvid3", "").strip()
            buvid4 = request.form.get("buvid4", "").strip()
            dedeuserid = request.form.get("dedeuserid", "").strip()
            ac_time_value = request.form.get("ac_time_value", "").strip()

            if action == "clear":
                _clear_user_credential(user)
                db.session.commit()
                flash("凭据已清空", "success")
                return redirect(url_for("user_credential"))

            payload = _build_credential_payload_from_form(
                user,
                cookie_input,
                sessdata,
                bili_jct,
                buvid3,
                buvid4,
                dedeuserid,
                ac_time_value,
            )

            if action == "test":
                ok, message = _test_credential_payload(user.uid, payload)
                if ok is True:
                    flash("凭据验证通过", "success")
                elif ok is False:
                    flash(f"凭据验证失败：{message}", "error")
                else:
                    flash("未填写凭据，无法测试", "error")
                view_user = _build_view_user(user, payload)
                return render_template(
                    "credential.html", user=view_user, cookie_value=cookie_input
                )

            if cookie_input:
                user.cookie = cookie_input
            user.sessdata = payload.get("sessdata", "")
            user.bili_jct = payload.get("bili_jct", "")
            user.buvid3 = payload.get("buvid3", "")
            user.buvid4 = payload.get("buvid4", "")
            user.dedeuserid = payload.get("dedeuserid", "")
            user.ac_time_value = payload.get("ac_time_value", "")

            db.session.commit()
            ok, message = _test_user_credential(user)
            if ok is True:
                flash("凭据已保存，验证通过", "success")
            elif ok is False:
                flash(f"凭据已保存，但验证失败：{message}", "error")
            else:
                flash("凭据已保存", "success")
            return redirect(url_for("user_credential"))

        global_interval = get_global_poll_interval()
        effective_interval = user.poll_interval or global_interval
        return render_template(
            "credential.html",
            user=user,
            poll_interval=user.poll_interval or 0,
            effective_interval=effective_interval,
            global_interval=global_interval,
        )

    @app.route("/admin/message", methods=["GET", "POST"])
    @admin_required
    def admin_message():
        bindings = (
            BiliBinding.query.join(BiliUser)
            .order_by(BiliUser.id.desc(), BiliBinding.id.desc())
            .all()
        )
        result = None
        error = None
        selected_id = None
        message = ""

        if request.method == "POST":
            selected_id = request.form.get("binding_id", "").strip()
            message = request.form.get("message", "").strip()

            if not selected_id:
                error = "请选择发送通道"
            elif not message:
                error = "消息内容不能为空"
            else:
                binding = BiliBinding.query.get_or_404(int(selected_id))
                if not binding.enable_onebot:
                    error = "该绑定未启用 OneBot 通知"
                    return render_template(
                        "message.html",
                        bindings=bindings,
                        result=result,
                        error=error,
                        selected_id=selected_id,
                        message=message,
                    )
                settings = _resolve_binding_settings(binding)

                result = onebot.send_text_with_result(settings, message, timeout=6)
                if result.get("ok"):
                    response = result.get("response") or {}
                    status = response.get("status")
                    retcode = response.get("retcode", 0)
                    if status and status != "ok":
                        error = response.get("message") or response.get("wording") or "发送失败"
                    elif retcode not in (0, "0"):
                        error = response.get("message") or response.get("wording") or "发送失败"
                else:
                    error = result.get("error") or "发送失败"

        return render_template(
            "message.html",
            bindings=bindings,
            result=result,
            error=error,
            selected_id=selected_id,
            message=message,
        )

    @app.route("/logs")
    @login_required
    def logs():
        level = request.args.get("level", "ALL").strip() or "ALL"
        logger_q = request.args.get("logger", "").strip()
        keyword = request.args.get("q", "").strip()
        uid = request.args.get("uid", "").strip()
        try:
            limit = int(request.args.get("limit", "200"))
        except Exception:
            limit = 200
        limit = max(20, min(500, limit))

        if current_user.is_admin:
            uid_filter = uid
        else:
            user = BiliUser.query.get(current_user.user_id)
            uid_filter = user.uid if user else ""

        entries = _read_log_entries(max_lines=3000)
        base_entries = entries
        entries = _filter_log_entries(entries, level, logger_q, keyword, uid_filter)
        entries = entries[-limit:]
        entries.reverse()

        logger_options = sorted({e.get("logger") for e in base_entries if e.get("logger")})
        uid_options = sorted({e.get("uid") for e in base_entries if e.get("uid")})

        return render_template(
            "logs.html",
            entries=entries,
            level=level,
            logger_q=logger_q,
            keyword=keyword,
            uid=uid_filter,
            uid_options=uid_options if current_user.is_admin else [],
            logger_options=logger_options,
            limit=limit,
        )

    @app.route("/bindings/test/<int:binding_id>", methods=["POST"])
    @login_required
    def binding_test(binding_id):
        binding = BiliBinding.query.get_or_404(binding_id)
        if not current_user.is_admin and binding.user_id != current_user.user_id:
            flash("无权限测试该绑定", "error")
            return redirect(url_for("user_bindings"))

        if not binding.enable_onebot:
            flash("该绑定未启用 OneBot 通知", "error")
            return redirect(_binding_edit_redirect(binding))

        test_type = request.form.get("test_type", "dynamic").strip()
        template = _get_binding_template(binding, test_type)
        image_bytes = None
        values = {}

        if test_type in ("dynamic", "video"):
            dynamic_id = request.form.get("dynamic_id", "").strip()
            info = _find_dynamic_for_test(binding.user, dynamic_id)
            if not info:
                flash("未找到指定动态，请刷新后重试", "error")
                return redirect(_binding_edit_redirect(binding))
            values = _build_dynamic_test_values(binding.user, info, test_type)
            if binding.enable_screenshot and "{SHOTPICTURE}" in template:
                image_bytes = _render_dynamic_test_image(binding, info)
        else:
            live_info = _fetch_live_test_info(binding.user)
            values, cover_url = _build_live_test_values(binding.user, live_info)
            if binding.enable_screenshot and "{SHOTPICTURE}" in template:
                image_bytes = _render_live_test_image(binding, values, cover_url)

        segments, rich = _build_segments(template, values, image_bytes)
        if not segments:
            flash("模板内容为空，无法发送", "error")
            return redirect(_binding_edit_redirect(binding))

        settings = _resolve_binding_settings(binding)
        if rich:
            onebot.send_segments(settings, segments)
        else:
            text = "".join(
                seg["data"]["text"] for seg in segments if seg.get("type") == "text"
            )
            if text:
                onebot.send_text(settings, text)

        flash("测试消息已发送", "success")
        return redirect(_binding_edit_redirect(binding))

    @app.route("/api/users")
    def api_users():
        users = BiliUser.query.order_by(BiliUser.id.desc()).all()
        global_interval = get_global_poll_interval()
        payload = []
        for u in users:
            status = get_status(u.id) or {}
            interval = u.poll_interval or global_interval
            next_poll_at = status.get("next_poll_at")
            if not next_poll_at and status.get("checked_at"):
                try:
                    checked = status.get("checked_at").replace("Z", "+00:00")
                    base = datetime.fromisoformat(checked)
                    next_poll_at = (base + timedelta(seconds=interval)).isoformat().replace("+00:00", "Z")
                except Exception:
                    next_poll_at = None
            payload.append(
                {
                    "id": u.id,
                    "uid": u.uid,
                    "name": u.name,
                    "live": status.get("live", False),
                    "live_title": status.get("live_title"),
                    "live_online": status.get("live_online"),
                    "live_duration": status.get("live_duration"),
                    "live_url": status.get("live_url"),
                    "last_dynamic_text": status.get("last_dynamic_text"),
                    "last_dynamic_time": status.get("last_dynamic_time"),
                    "last_dynamic_url": status.get("last_dynamic_url"),
                    "last_dynamic_id": status.get("last_dynamic_id"),
                    "last_dynamic_title": status.get("last_dynamic_title"),
                    "last_dynamic_type": status.get("last_dynamic_type"),
                    "last_dynamic_video_url": status.get("last_dynamic_video_url"),
                    "last_dynamic_cover": status.get("last_dynamic_cover"),
                    "last_dynamic_is_video": status.get("last_dynamic_is_video"),
                    "checked_at": status.get("checked_at"),
                    "poll_interval": status.get("poll_interval") or interval,
                    "next_poll_at": next_poll_at,
                }
            )
        return jsonify(payload)

    return app


def _find_user_by_login(login_name: str) -> BiliUser | None:
    if not login_name:
        return None
    user = BiliUser.query.filter_by(login_username=login_name).first()
    if user:
        return user
    return BiliUser.query.filter_by(uid=login_name).first()


def _parse_cookie(cookie: str) -> dict:
    if not cookie:
        return {}
    data = {}
    keys = (
        "SESSDATA",
        "bili_jct",
        "buvid3",
        "buvid4",
        "DedeUserID",
        "ac_time_value",
        "ac_time",
    )
    for key in keys:
        match = re.search(rf"(?:^|;)\s*{re.escape(key)}=([^;]*)", cookie)
        if match:
            data[key] = match.group(1)
    return data


def _clear_user_credential(user: BiliUser):
    user.cookie = ""
    user.sessdata = ""
    user.bili_jct = ""
    user.buvid3 = ""
    user.buvid4 = ""
    user.dedeuserid = ""
    user.ac_time_value = ""


def _build_user_credential_payload(user: BiliUser) -> dict:
    return {
        "cookie": user.cookie or "",
        "sessdata": user.sessdata or "",
        "bili_jct": user.bili_jct or "",
        "buvid3": user.buvid3 or "",
        "buvid4": user.buvid4 or "",
        "dedeuserid": user.dedeuserid or "",
        "ac_time_value": user.ac_time_value or "",
    }


def _build_credential_payload_from_form(
    user: BiliUser,
    cookie_input: str,
    sessdata: str,
    bili_jct: str,
    buvid3: str,
    buvid4: str,
    dedeuserid: str,
    ac_time_value: str,
) -> dict:
    payload = _build_user_credential_payload(user)
    parsed = _parse_cookie(cookie_input) if cookie_input else {}
    if cookie_input:
        payload["cookie"] = cookie_input
    if sessdata or parsed.get("SESSDATA"):
        payload["sessdata"] = sessdata or parsed.get("SESSDATA") or payload["sessdata"]
    if bili_jct or parsed.get("bili_jct"):
        payload["bili_jct"] = bili_jct or parsed.get("bili_jct") or payload["bili_jct"]
    if buvid3 or parsed.get("buvid3"):
        payload["buvid3"] = buvid3 or parsed.get("buvid3") or payload["buvid3"]
    if buvid4 or parsed.get("buvid4"):
        payload["buvid4"] = buvid4 or parsed.get("buvid4") or payload["buvid4"]
    if dedeuserid or parsed.get("DedeUserID"):
        payload["dedeuserid"] = (
            dedeuserid or parsed.get("DedeUserID") or payload["dedeuserid"]
        )
    parsed_actime = parsed.get("ac_time_value") or parsed.get("ac_time")
    if ac_time_value or parsed_actime:
        payload["ac_time_value"] = (
            ac_time_value or parsed_actime or payload["ac_time_value"]
        )
    return payload


def _test_credential_payload(uid: str, payload: dict) -> tuple[bool | None, str]:
    if not uid:
        return False, "UID 为空"
    if not any(payload.values()):
        return None, "empty"
    try:
        data = fetch_user_info(uid, payload)
        if data:
            return True, ""
        return False, "未返回用户信息"
    except Exception as exc:
        return False, str(exc)


def _build_view_user(
    user: BiliUser,
    payload: dict,
    uid: str | None = None,
    name: str | None = None,
    login_username: str | None = None,
    enabled: bool | None = None,
    poll_interval: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user.id,
        uid=uid or user.uid,
        name=name if name is not None else user.name,
        login_username=login_username if login_username is not None else user.login_username,
        enabled=enabled if enabled is not None else user.enabled,
        poll_interval=poll_interval if poll_interval is not None else user.poll_interval,
        sessdata=payload.get("sessdata", ""),
        bili_jct=payload.get("bili_jct", ""),
        buvid3=payload.get("buvid3", ""),
        buvid4=payload.get("buvid4", ""),
        dedeuserid=payload.get("dedeuserid", ""),
        ac_time_value=payload.get("ac_time_value", ""),
    )


def _read_log_entries(max_lines: int = 2000) -> list[dict]:
    entries = []
    try:
        rows = (
            BiliLogEntry.query.order_by(BiliLogEntry.id.desc())
            .limit(max_lines)
            .all()
        )
        for row in reversed(rows):
            item = {
                "time": row.time or "",
                "level": row.level or "INFO",
                "logger": row.logger or "",
                "message": row.message or "",
                "uid": row.uid or "",
                "module": row.module or "",
                "line": row.line or "",
                "thread": row.thread or "",
            }
            if row.payload:
                try:
                    payload = json.loads(row.payload)
                    if isinstance(payload, dict):
                        item.update(payload)
                except Exception:
                    pass
            entries.append(item)
        return entries
    except Exception:
        entries = []
    if not LOG_FILE or not os.path.exists(LOG_FILE):
        return entries
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as handle:
        lines = deque(handle, maxlen=max_lines)
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                entries.append(payload)
        except Exception:
            entries.append(
                {
                    "time": "",
                    "level": "INFO",
                    "logger": "raw",
                    "message": raw,
                    "uid": "",
                }
            )
    return entries


def _filter_log_entries(
    entries: list[dict],
    level: str,
    logger_q: str,
    keyword: str,
    uid: str,
) -> list[dict]:
    filtered = []
    for item in entries:
        if uid:
            item_uid = str(item.get("uid") or "")
            if item_uid != str(uid):
                message = str(item.get("message") or "")
                if str(uid) not in message:
                    continue
        if level and level != "ALL" and item.get("level") != level:
            continue
        if logger_q and logger_q.lower() not in str(item.get("logger") or "").lower():
            continue
        if keyword and keyword.lower() not in str(item.get("message") or "").lower():
            continue
        filtered.append(item)
    return filtered


def _test_user_credential(user: BiliUser) -> tuple[bool | None, str]:
    payload = _build_user_credential_payload(user)
    if not any(payload.values()):
        return None, "empty"
    try:
        data = fetch_user_info(user.uid, payload)
        if data:
            return True, ""
        return False, "未返回用户信息"
    except Exception as exc:
        return False, str(exc)


def _resolve_binding_settings(binding: BiliBinding) -> dict:
    profile = binding.onebot_profile
    ws_url = profile.ws_url if profile and profile.ws_url else binding.onebot_ws_url
    access_token = (
        profile.access_token if profile and profile.access_token else binding.onebot_access_token
    )
    return {
        "onebot_ws_url": ws_url,
        "onebot_access_token": access_token,
        "onebot_target_type": binding.onebot_target_type,
        "onebot_target_id": binding.onebot_target_id,
    }


def _binding_edit_redirect(binding: BiliBinding):
    if getattr(current_user, "is_admin", False):
        return url_for("admin_binding_edit", binding_id=binding.id)
    return url_for("user_binding_edit", binding_id=binding.id)


def _get_binding_template(binding: BiliBinding, key: str) -> str:
    template = getattr(binding, f"template_{key}", "") or ""
    if template:
        return template
    return DEFAULT_TEMPLATES.get(key, "")


def _get_recent_dynamics(user: BiliUser, limit: int = 10) -> list[dict]:
    items = fetch_dynamic_list(user.uid, credential_data=_build_user_credential_payload(user))
    if not items:
        return []
    results = []
    for item in items:
        info = _parse_dynamic_item(item)
        if not info:
            continue
        label = _format_dynamic_label(info)
        results.append({"id": info["id"], "label": label})
        if len(results) >= limit:
            break
    return results


def _find_dynamic_for_test(user: BiliUser, dynamic_id: str) -> dict | None:
    if not dynamic_id:
        return None
    items = fetch_dynamic_list(user.uid, credential_data=_build_user_credential_payload(user))
    if not items:
        return None
    for item in items:
        info = _parse_dynamic_item(item)
        if info and info.get("id") == dynamic_id:
            return info
    return None


def _parse_dynamic_item(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    dyn_id = item.get("id_str") or item.get("id")
    if dyn_id is None:
        return None
    dyn_id = str(dyn_id)
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    pub_ts = author.get("pub_ts") or author.get("pub_time") or 0

    dynamic = modules.get("module_dynamic") or {}
    text = _extract_desc_text(dynamic)
    desc_module = _pick_desc_module(modules)
    if not text:
        text = _extract_module_desc_text(desc_module)

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
        video_url = _normalize_url(video_url)
        if video_url and not video_url.startswith("http") and video_url.startswith("BV"):
            video_url = f"https://www.bilibili.com/video/{video_url}"

    avatar, _ = _extract_author_media(author)
    images, extra = _extract_dynamic_media(dynamic)
    orig = item.get("orig") or item.get("origin")
    if isinstance(orig, dict):
        orig_dynamic = (orig.get("modules") or {}).get("module_dynamic") or {}
        if isinstance(orig_dynamic, dict):
            orig_images, orig_extra = _extract_dynamic_media(orig_dynamic)
            if orig_images:
                images.extend(orig_images)
            if orig_extra and not extra:
                extra = orig_extra
            if not text:
                text = _extract_desc_text(orig_dynamic)
    url = f"https://t.bilibili.com/{dyn_id}"
    return {
        "id": dyn_id,
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
    }


def _format_dynamic_label(info: dict) -> str:
    ts = info.get("time") or 0
    time_label = _format_timestamp(ts)
    title = info.get("video_title") if info.get("is_video") else info.get("text")
    title = _short_text(title or "动态", 48)
    tag = "视频" if info.get("is_video") else "动态"
    if time_label:
        return f"{time_label} · {tag} · {title}"
    return f"{tag} · {title}"


def _extract_dynamic_media(dynamic: dict) -> tuple[list[str], dict]:
    images = []
    extra = {}
    major = dynamic.get("major") or {}
    if not isinstance(major, dict):
        return images, extra
    images.extend(_collect_image_urls(major))
    extra = _extract_extra_card(major)
    return images, extra


def _collect_image_urls(major: dict) -> list[str]:
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
                images.append(_normalize_url(str(url)))

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
                    images.append(_normalize_url(str(url)))

    article = major.get("article")
    if isinstance(article, dict):
        covers = article.get("covers") or []
        if isinstance(covers, list):
            for cover in covers:
                if cover:
                    images.append(_normalize_url(str(cover)))
        cover = article.get("cover")
        if cover:
            images.append(_normalize_url(str(cover)))

    common = major.get("common")
    if isinstance(common, dict):
        cover = common.get("cover") or ""
        if cover:
            images.append(_normalize_url(str(cover)))

    ugc = major.get("ugc_season")
    if isinstance(ugc, dict):
        cover = ugc.get("cover") or ""
        if cover:
            images.append(_normalize_url(str(cover)))

    live = major.get("live_rcmd")
    if isinstance(live, dict):
        cover = live.get("cover") or ""
        if cover:
            images.append(_normalize_url(str(cover)))

    pgc = major.get("pgc")
    if isinstance(pgc, dict):
        cover = pgc.get("cover") or pgc.get("cover_url") or ""
        if cover:
            images.append(_normalize_url(str(cover)))

    music = major.get("music")
    if isinstance(music, dict):
        cover = music.get("cover") or music.get("cover_url") or ""
        if cover:
            images.append(_normalize_url(str(cover)))

    reserve = major.get("reserve")
    if isinstance(reserve, dict):
        cover = reserve.get("cover") or reserve.get("image") or ""
        if cover:
            images.append(_normalize_url(str(cover)))

    cleaned = []
    seen = set()
    for url in images:
        if not url or url in seen:
            continue
        seen.add(url)
        cleaned.append(url)
    return cleaned


def _extract_extra_card(major: dict) -> dict:
    common = major.get("common")
    if isinstance(common, dict):
        return {
            "title": common.get("title") or common.get("name") or "",
            "desc": common.get("desc") or common.get("summary") or "",
            "url": _normalize_url(common.get("jump_url") or common.get("url") or ""),
            "cover": _normalize_url(common.get("cover") or ""),
        }
    article = major.get("article")
    if isinstance(article, dict):
        return {
            "title": article.get("title") or "",
            "desc": article.get("desc") or article.get("summary") or "",
            "url": _normalize_url(article.get("jump_url") or article.get("url") or ""),
            "cover": _normalize_url(article.get("cover") or ""),
        }
    archive = major.get("archive")
    if isinstance(archive, dict):
        return {
            "title": archive.get("title") or "",
            "desc": archive.get("desc") or archive.get("desc_text") or "",
            "url": _normalize_url(archive.get("jump_url") or archive.get("url") or ""),
            "cover": _normalize_url(archive.get("cover") or ""),
        }
    live = major.get("live_rcmd") or major.get("live")
    if isinstance(live, dict):
        return {
            "title": live.get("title") or live.get("roomname") or "",
            "desc": live.get("desc") or live.get("intro") or "",
            "url": _normalize_url(live.get("link") or live.get("url") or ""),
            "cover": _normalize_url(live.get("cover") or live.get("keyframe") or ""),
        }
    reserve = major.get("reserve")
    if isinstance(reserve, dict):
        return {
            "title": reserve.get("title") or "",
            "desc": reserve.get("desc") or reserve.get("desc1") or reserve.get("desc2") or "",
            "url": _normalize_url(reserve.get("jump_url") or ""),
            "cover": _normalize_url(reserve.get("cover") or ""),
        }
    opus = major.get("opus")
    if isinstance(opus, dict):
        return {
            "title": opus.get("title") or "",
            "desc": opus.get("summary") or opus.get("content") or "",
            "url": _normalize_url(opus.get("jump_url") or opus.get("url") or ""),
            "cover": _normalize_url(opus.get("cover") or ""),
        }
    topic = major.get("topic")
    if isinstance(topic, dict):
        return {
            "title": topic.get("title") or topic.get("name") or "",
            "desc": topic.get("desc") or topic.get("summary") or "",
            "url": _normalize_url(topic.get("jump_url") or topic.get("url") or ""),
            "cover": _normalize_url(topic.get("cover") or topic.get("image") or ""),
        }
    medialist = major.get("medialist") or major.get("collection") or major.get("fav")
    if isinstance(medialist, dict):
        return {
            "title": medialist.get("title") or medialist.get("name") or "",
            "desc": medialist.get("desc") or medialist.get("summary") or "",
            "url": _normalize_url(medialist.get("jump_url") or medialist.get("url") or ""),
            "cover": _normalize_url(medialist.get("cover") or medialist.get("image") or ""),
        }
    activity = major.get("activity") or major.get("mission") or major.get("courses")
    if isinstance(activity, dict):
        return {
            "title": activity.get("title") or activity.get("name") or "",
            "desc": activity.get("desc") or activity.get("summary") or "",
            "url": _normalize_url(activity.get("jump_url") or activity.get("url") or ""),
            "cover": _normalize_url(activity.get("cover") or activity.get("image") or ""),
        }
    return {}


def _build_media_html(images: list[str], extra: dict) -> str:
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
        title = _stringify(extra.get("title"))
        desc = _stringify(extra.get("desc"))
        url = _stringify(extra.get("url"))
        cover = _stringify(extra.get("cover"))
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
                card.append(f'<div class="media-title">{html_lib.escape(title)}</div>')
            if desc:
                card.append(f'<div class="media-desc">{html_lib.escape(desc)}</div>')
            if url:
                card.append(f'<div class="media-link">{html_lib.escape(url)}</div>')
            parts.append(f'<div class="media-card">{"".join(card)}</div>')

    return "".join(parts)


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


def _extract_module_desc_text(module: dict | None) -> str:
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


def _extract_author_media(author: dict) -> tuple[str, str]:
    avatar = ""
    if isinstance(author, dict):
        avatar = author.get("face") or author.get("avatar") or ""
    return _normalize_url(str(avatar)) if avatar else "", ""


def _fetch_user_avatar(user: BiliUser) -> tuple[str, str]:
    info = fetch_user_info(user.uid, _build_user_credential_payload(user))
    if not info:
        return "", ""
    avatar = info.get("face") or info.get("avatar") or ""
    return _normalize_url(str(avatar)) if avatar else "", ""


def _build_dynamic_test_values(user: BiliUser, info: dict, test_type: str) -> dict:
    name = user.name or f"UID {user.uid}"
    text = info.get("text") or ""
    title = info.get("video_title") or ""
    url = info.get("video_url") or info.get("url") or ""
    if test_type == "video" and not title:
        title = "动态未包含视频"
    return {
        "name": name,
        "text": text,
        "title": title,
        "url": url,
        "online": "",
        "duration": "",
        "max_online": "",
    }


def _fetch_live_test_info(user: BiliUser) -> dict | None:
    return fetch_live_info(user.uid, _build_user_credential_payload(user))


def _build_live_test_values(user: BiliUser, info: dict | None) -> tuple[dict, str]:
    name = user.name or f"UID {user.uid}"
    title = ""
    online = ""
    url = ""
    duration = ""
    max_online = ""
    cover_url = ""
    room_id = ""
    if isinstance(info, dict):
        title = info.get("title") or info.get("roomname") or ""
        online = info.get("online") or info.get("online_num") or ""
        room_id = info.get("roomid") or info.get("room_id") or ""
        if room_id:
            url = f"https://live.bilibili.com/{room_id}"
        start_ts = info.get("live_time") or info.get("start_time") or 0
        if start_ts:
            duration = format_duration(time.time() - float(start_ts))
        max_online = online or ""
        cover_url = (
            info.get("keyframe")
            or info.get("live_screen")
            or info.get("cover")
            or info.get("cover_from_user")
            or info.get("user_cover")
            or ""
        )

    if not cover_url:
        cover_url = fetch_live_room_cover(
            user.uid,
            room_id=room_id,
            credential_data=_build_user_credential_payload(user),
        )

    if not title:
        title = "模拟开播"
    if not url:
        url = "https://live.bilibili.com"
    if not online:
        online = "12345"
    if not duration:
        duration = "1h23m"
    if not max_online:
        max_online = online

    avatar, _ = _fetch_user_avatar(user)
    return (
        {
            "name": name,
            "title": title,
            "online": online,
            "url": url,
            "duration": duration,
            "max_online": max_online,
            "text": "",
            "avatar": avatar,
        },
        cover_url,
    )


def _render_dynamic_test_image(binding: BiliBinding, info: dict) -> bytes | None:
    html_template = get_screenshot_template_value(binding.id, "dynamic")
    name = binding.user.name or f"UID {binding.user.uid}"
    if not info.get("avatar"):
        avatar, _ = _fetch_user_avatar(binding.user)
        if avatar:
            info["avatar"] = avatar
    html_values = _dynamic_html_values(name, info)
    image = _render_html_image(html_template, html_values)
    if image:
        return image
    cover_url = info.get("cover_url") or ""
    if cover_url:
        return download_image(cover_url)
    return None


def _render_live_test_image(
    binding: BiliBinding, values: dict, cover_url: str
) -> bytes | None:
    html_template = get_screenshot_template_value(binding.id, "live")
    name = values.get("name") or binding.user.name or f"UID {binding.user.uid}"
    if not values.get("avatar"):
        avatar, _ = _fetch_user_avatar(binding.user)
        if avatar:
            values["avatar"] = avatar
    html_values = _live_html_values(name, values, cover_url)
    image = _render_html_image(html_template, html_values)
    if image:
        return image
    if cover_url:
        return download_image(cover_url)
    return None


def _dynamic_html_values(name: str, info: dict) -> dict:
    is_video = bool(info.get("is_video"))
    title = info.get("video_title") or ""
    if not title and not is_video:
        title = "发布了新动态"
    url = info.get("video_url") or info.get("url") or ""
    images = info.get("images") or []
    extra = info.get("extra") or {}
    media_html = info.get("media_html") or _build_media_html(images, extra)
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
        "name_initial": _name_initial(name),
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


def _live_html_values(name: str, values: dict, cover_url: str) -> dict:
    cover = cover_url or ""
    avatar = values.get("avatar") or ""
    return {
        "name": values.get("name") or name,
        "name_initial": _name_initial(name),
        "text": "",
        "title": values.get("title") or "",
        "url": values.get("url") or "",
        "online": values.get("online") or "",
        "duration": values.get("duration") or "",
        "max_online": values.get("max_online") or "",
        "cover": cover,
        "cover_display": "block" if cover else "none",
        "avatar": avatar,
        "avatar_display": "block" if avatar else "none",
        "avatar_text_display": "none" if avatar else "block",
        "image_count": 0,
        "media_html": "",
    }


def _render_html_image(template: str, values: dict) -> bytes | None:
    if not template:
        return None
    html = render_html_template(template, values)
    return render_html_to_image(html)


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


def _format_timestamp(ts: int | float | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _short_text(text: str, limit: int) -> str:
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def _name_initial(name: str) -> str:
    if not name:
        return ""
    trimmed = str(name).strip()
    if not trimmed:
        return ""
    return trimmed[:2]


def _build_segments(template: str, values: dict, image_bytes: bytes | None):
    parts = _SPECIAL_PATTERN.split(template)
    segments = []
    rich = False
    for part in parts:
        if not part:
            continue
        if part == "{SHOTPICTURE}":
            if image_bytes:
                segments.append(_image_segment(image_bytes))
                rich = True
            continue
        if part == "[atALL]":
            segments.append({"type": "at", "data": {"qq": "all"}})
            rich = True
            continue
        text = _apply_values(part, values)
        if text:
            segments.append({"type": "text", "data": {"text": text}})
    if any(seg.get("type") != "text" for seg in segments):
        rich = True
    return segments, rich


def _apply_values(text: str, values: dict) -> str:
    if not text:
        return ""
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", "" if value is None else str(value))
    return text


def _image_segment(image_bytes: bytes) -> dict:
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    return {"type": "image", "data": {"file": f"base64://{image_b64}"}}


def _parse_live_hourly_interval(raw: str) -> int:
    default_minutes = max(30, get_live_hourly_interval_minutes())
    if not raw:
        return default_minutes
    try:
        value = int(raw)
    except Exception:
        return default_minutes
    if value < 30:
        return 30
    return value


def _build_binding_from_form(user_id: int) -> BiliBinding:
    name = request.form.get("name", "").strip() or "默认"
    onebot_profile_id = request.form.get("onebot_profile_id", "").strip()
    onebot_ws_url = request.form.get("onebot_ws_url", "").strip()
    onebot_access_token = request.form.get("onebot_access_token", "").strip()
    onebot_target_type = request.form.get("onebot_target_type", "group").strip()
    onebot_target_id = request.form.get("onebot_target_id", "").strip()
    live_hourly_interval = _parse_live_hourly_interval(
        request.form.get("live_hourly_interval", "").strip()
    )

    enable_onebot = bool(request.form.get("enable_onebot"))
    notify_dynamic = bool(request.form.get("notify_dynamic"))
    notify_video = bool(request.form.get("notify_video"))
    notify_live_start = bool(request.form.get("notify_live_start"))
    notify_live_hourly = bool(request.form.get("notify_live_hourly"))
    notify_live_end = bool(request.form.get("notify_live_end"))
    enable_screenshot = bool(request.form.get("enable_screenshot"))

    template_dynamic = request.form.get("template_dynamic", "").strip()
    template_video = request.form.get("template_video", "").strip()
    template_live_start = request.form.get("template_live_start", "").strip()
    template_live_hourly = request.form.get("template_live_hourly", "").strip()
    template_live_end = request.form.get("template_live_end", "").strip()
    if onebot_profile_id == "":
        onebot_profile_id = None
    else:
        try:
            onebot_profile_id = int(onebot_profile_id)
        except ValueError:
            onebot_profile_id = None

    return BiliBinding(
        user_id=user_id,
        name=name,
        onebot_profile_id=onebot_profile_id,
        onebot_ws_url=onebot_ws_url,
        onebot_access_token=onebot_access_token,
        onebot_target_type=onebot_target_type or "group",
        onebot_target_id=onebot_target_id,
        enable_onebot=enable_onebot,
        notify_dynamic=notify_dynamic,
        notify_video=notify_video,
        notify_live_start=notify_live_start,
        notify_live_hourly=notify_live_hourly,
        notify_live_end=notify_live_end,
        enable_screenshot=enable_screenshot,
        live_hourly_interval=live_hourly_interval,
        template_dynamic=template_dynamic,
        template_video=template_video,
        template_live_start=template_live_start,
        template_live_hourly=template_live_hourly,
        template_live_end=template_live_end,
    )


def _update_binding_from_form(binding: BiliBinding):
    binding.name = request.form.get("name", "").strip() or "默认"
    onebot_profile_id = request.form.get("onebot_profile_id", "").strip()
    if onebot_profile_id:
        try:
            binding.onebot_profile_id = int(onebot_profile_id)
        except ValueError:
            binding.onebot_profile_id = None
    else:
        binding.onebot_profile_id = None
    binding.onebot_ws_url = request.form.get("onebot_ws_url", "").strip()
    binding.onebot_access_token = request.form.get("onebot_access_token", "").strip()
    binding.onebot_target_type = request.form.get("onebot_target_type", "group").strip() or "group"
    binding.onebot_target_id = request.form.get("onebot_target_id", "").strip()
    binding.live_hourly_interval = _parse_live_hourly_interval(
        request.form.get("live_hourly_interval", "").strip()
    )

    binding.enable_onebot = bool(request.form.get("enable_onebot"))
    binding.notify_dynamic = bool(request.form.get("notify_dynamic"))
    binding.notify_video = bool(request.form.get("notify_video"))
    binding.notify_live_start = bool(request.form.get("notify_live_start"))
    binding.notify_live_hourly = bool(request.form.get("notify_live_hourly"))
    binding.notify_live_end = bool(request.form.get("notify_live_end"))
    binding.enable_screenshot = bool(request.form.get("enable_screenshot"))

    binding.template_dynamic = request.form.get("template_dynamic", "").strip()
    binding.template_video = request.form.get("template_video", "").strip()
    binding.template_live_start = request.form.get("template_live_start", "").strip()
    binding.template_live_hourly = request.form.get("template_live_hourly", "").strip()
    binding.template_live_end = request.form.get("template_live_end", "").strip()


def _read_screenshot_templates_from_form() -> tuple[str, str]:
    template_dynamic = request.form.get("screenshot_template_dynamic", "").strip()
    template_live = request.form.get("screenshot_template_live", "").strip()
    return template_dynamic, template_live


def _ensure_user_columns():
    result = db.session.execute(text("PRAGMA table_info(bili_users)"))
    existing = {row[1] for row in result.fetchall()}
    if not existing:
        db.create_all()
        result = db.session.execute(text("PRAGMA table_info(bili_users)"))
        existing = {row[1] for row in result.fetchall()}
    expected = {
        "uid": "TEXT",
        "name": "TEXT",
        "enabled": "INTEGER",
        "login_username": "TEXT",
        "password_hash": "TEXT",
        "cookie": "TEXT",
        "sessdata": "TEXT",
        "bili_jct": "TEXT",
        "buvid3": "TEXT",
        "buvid4": "TEXT",
        "dedeuserid": "TEXT",
        "ac_time_value": "TEXT",
        "poll_interval": "INTEGER",
    }
    for name, coltype in expected.items():
        if name not in existing:
            db.session.execute(text(f"ALTER TABLE bili_users ADD COLUMN {name} {coltype}"))
    db.session.execute(text("UPDATE bili_users SET enabled=1 WHERE enabled IS NULL"))
    db.session.execute(
        text("UPDATE bili_users SET login_username=uid WHERE login_username IS NULL OR login_username='' ")
    )
    db.session.execute(
        text("UPDATE bili_users SET password_hash='' WHERE password_hash IS NULL")
    )
    db.session.execute(text("UPDATE bili_users SET cookie='' WHERE cookie IS NULL"))
    db.session.execute(text("UPDATE bili_users SET sessdata='' WHERE sessdata IS NULL"))
    db.session.execute(text("UPDATE bili_users SET bili_jct='' WHERE bili_jct IS NULL"))
    db.session.execute(text("UPDATE bili_users SET buvid3='' WHERE buvid3 IS NULL"))
    db.session.execute(text("UPDATE bili_users SET buvid4='' WHERE buvid4 IS NULL"))
    db.session.execute(text("UPDATE bili_users SET dedeuserid='' WHERE dedeuserid IS NULL"))
    db.session.execute(text("UPDATE bili_users SET ac_time_value='' WHERE ac_time_value IS NULL"))
    db.session.execute(text("UPDATE bili_users SET poll_interval=0 WHERE poll_interval IS NULL"))
    db.session.commit()


def _ensure_onebot_profile_columns():
    result = db.session.execute(text("PRAGMA table_info(onebot_profiles)"))
    existing = {row[1] for row in result.fetchall()}
    if not existing:
        db.create_all()
        result = db.session.execute(text("PRAGMA table_info(onebot_profiles)"))
        existing = {row[1] for row in result.fetchall()}
    expected = {
        "name": "TEXT",
        "ws_url": "TEXT",
        "access_token": "TEXT",
    }
    for name, coltype in expected.items():
        if name not in existing:
            db.session.execute(text(f"ALTER TABLE onebot_profiles ADD COLUMN {name} {coltype}"))
    db.session.commit()


def _ensure_binding_columns():
    result = db.session.execute(text("PRAGMA table_info(bili_bindings)"))
    existing = {row[1] for row in result.fetchall()}
    if not existing:
        db.create_all()
        result = db.session.execute(text("PRAGMA table_info(bili_bindings)"))
        existing = {row[1] for row in result.fetchall()}
    expected = {
        "user_id": "INTEGER",
        "name": "TEXT",
        "onebot_profile_id": "INTEGER",
        "onebot_ws_url": "TEXT",
        "onebot_access_token": "TEXT",
        "onebot_target_type": "TEXT",
        "onebot_target_id": "TEXT",
        "enable_onebot": "INTEGER",
        "notify_dynamic": "INTEGER",
        "notify_video": "INTEGER",
        "notify_live_start": "INTEGER",
        "notify_live_hourly": "INTEGER",
        "notify_live_end": "INTEGER",
        "enable_screenshot": "INTEGER",
        "live_hourly_interval": "INTEGER",
        "template_dynamic": "TEXT",
        "template_video": "TEXT",
        "template_live_start": "TEXT",
        "template_live_hourly": "TEXT",
        "template_live_end": "TEXT",
        "screenshot_template_dynamic": "TEXT",
        "screenshot_template_live": "TEXT",
    }
    for name, coltype in expected.items():
        if name not in existing:
            db.session.execute(text(f"ALTER TABLE bili_bindings ADD COLUMN {name} {coltype}"))

    db.session.execute(text("UPDATE bili_bindings SET enable_onebot=1 WHERE enable_onebot IS NULL"))
    db.session.execute(text("UPDATE bili_bindings SET notify_dynamic=1 WHERE notify_dynamic IS NULL"))
    db.session.execute(text("UPDATE bili_bindings SET notify_video=1 WHERE notify_video IS NULL"))
    db.session.execute(
        text("UPDATE bili_bindings SET notify_live_start=1 WHERE notify_live_start IS NULL")
    )
    db.session.execute(
        text("UPDATE bili_bindings SET notify_live_hourly=1 WHERE notify_live_hourly IS NULL")
    )
    db.session.execute(
        text("UPDATE bili_bindings SET notify_live_end=1 WHERE notify_live_end IS NULL")
    )
    if "enable_dynamic_screenshot" in existing:
        db.session.execute(
            text(
                "UPDATE bili_bindings SET enable_screenshot=enable_dynamic_screenshot "
                "WHERE enable_screenshot IS NULL"
            )
        )
    db.session.execute(
        text("UPDATE bili_bindings SET enable_screenshot=1 WHERE enable_screenshot IS NULL")
    )
    if "template_dynamic" in existing:
        db.session.execute(
            text(
                "UPDATE bili_bindings SET template_dynamic=:val "
                "WHERE template_dynamic IS NULL OR template_dynamic=''"
            ),
            {"val": DEFAULT_TEMPLATES["dynamic"]},
        )
        db.session.execute(
            text(
                "UPDATE bili_bindings SET template_video=:val "
                "WHERE template_video IS NULL OR template_video=''"
            ),
            {"val": DEFAULT_TEMPLATES["video"]},
        )
        db.session.execute(
            text(
                "UPDATE bili_bindings SET template_live_start=:val "
                "WHERE template_live_start IS NULL OR template_live_start=''"
            ),
            {"val": DEFAULT_TEMPLATES["live_start"]},
        )
        db.session.execute(
            text(
                "UPDATE bili_bindings SET template_live_hourly=:val "
                "WHERE template_live_hourly IS NULL OR template_live_hourly=''"
            ),
            {"val": DEFAULT_TEMPLATES["live_hourly"]},
        )
        db.session.execute(
            text(
                "UPDATE bili_bindings SET template_live_end=:val "
                "WHERE template_live_end IS NULL OR template_live_end=''"
            ),
            {"val": DEFAULT_TEMPLATES["live_end"]},
        )
    default_live_minutes = max(30, get_live_hourly_interval_minutes())
    db.session.execute(
        text(
            "UPDATE bili_bindings SET live_hourly_interval=:val "
            "WHERE live_hourly_interval IS NULL OR live_hourly_interval=0"
        ),
        {"val": default_live_minutes},
    )
    db.session.execute(
        text("UPDATE bili_bindings SET live_hourly_interval=30 WHERE live_hourly_interval < 30")
    )
    db.session.commit()


def _ensure_screenshot_template_records():
    try:
        bindings = BiliBinding.query.all()
        for binding in bindings:
            ensure_screenshot_templates(
                binding.id,
                binding.screenshot_template_dynamic or "",
                binding.screenshot_template_live or "",
            )
    except Exception:
        return


def _seed_bindings():
    users = BiliUser.query.all()
    for user in users:
        if user.bindings:
            continue
        binding = BiliBinding(
            user_id=user.id,
            name="默认",
            onebot_profile_id=None,
            onebot_ws_url="",
            onebot_access_token="",
            onebot_target_type="group",
            onebot_target_id="",
            enable_onebot=True,
            notify_dynamic=True,
            notify_video=True,
            notify_live_start=True,
            notify_live_hourly=True,
            notify_live_end=True,
            enable_screenshot=True,
            live_hourly_interval=max(30, get_live_hourly_interval_minutes()),
            template_dynamic=DEFAULT_TEMPLATES["dynamic"],
            template_video=DEFAULT_TEMPLATES["video"],
            template_live_start=DEFAULT_TEMPLATES["live_start"],
            template_live_hourly=DEFAULT_TEMPLATES["live_hourly"],
            template_live_end=DEFAULT_TEMPLATES["live_end"],
        )
        db.session.add(binding)
        db.session.flush()
        save_screenshot_templates(
            binding.id,
            DEFAULT_HTML_TEMPLATES["dynamic"],
            DEFAULT_HTML_TEMPLATES["live"],
        )
    db.session.commit()


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
