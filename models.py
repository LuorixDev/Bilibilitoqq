from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


class BiliUser(db.Model):
    __tablename__ = "bili_users"

    id = db.Column(db.Integer, primary_key=True)
    uid = db.Column(db.String(32), nullable=False, unique=True)
    name = db.Column(db.String(120), nullable=False, default="")
    enabled = db.Column(db.Boolean, default=True)
    login_username = db.Column(db.String(64), nullable=False, default="")
    password_hash = db.Column(db.String(255), nullable=False, default="")
    cookie = db.Column(db.Text, default="")
    sessdata = db.Column(db.String(255), default="")
    bili_jct = db.Column(db.String(255), default="")
    buvid3 = db.Column(db.String(255), default="")
    buvid4 = db.Column(db.String(255), default="")
    dedeuserid = db.Column(db.String(255), default="")
    ac_time_value = db.Column(db.String(255), default="")
    poll_interval = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class OneBotProfile(db.Model):
    __tablename__ = "onebot_profiles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="默认")
    ws_url = db.Column(db.String(255), nullable=False, default="")
    access_token = db.Column(db.String(255), nullable=False, default="")
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class BiliBinding(db.Model):
    __tablename__ = "bili_bindings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("bili_users.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False, default="默认")

    onebot_profile_id = db.Column(
        db.Integer,
        db.ForeignKey("onebot_profiles.id"),
        nullable=True,
        index=True,
    )
    onebot_ws_url = db.Column(db.String(255), default="")
    onebot_access_token = db.Column(db.String(255), default="")
    onebot_target_type = db.Column(db.String(20), default="group")
    onebot_target_id = db.Column(db.String(50), default="")
    enable_onebot = db.Column(db.Boolean, default=True)

    notify_dynamic = db.Column(db.Boolean, default=True)
    notify_video = db.Column(db.Boolean, default=True)
    notify_live_start = db.Column(db.Boolean, default=True)
    notify_live_hourly = db.Column(db.Boolean, default=True)
    notify_live_end = db.Column(db.Boolean, default=True)
    enable_screenshot = db.Column(db.Boolean, default=True)
    template_dynamic = db.Column(db.Text, default="")
    template_video = db.Column(db.Text, default="")
    template_live_start = db.Column(db.Text, default="")
    template_live_hourly = db.Column(db.Text, default="")
    template_live_end = db.Column(db.Text, default="")
    screenshot_template_dynamic = db.Column(db.Text, default="")
    screenshot_template_live = db.Column(db.Text, default="")

    created_at = db.Column(db.DateTime, server_default=db.func.now())

    user = db.relationship(
        "BiliUser",
        backref=db.backref("bindings", lazy=True, cascade="all, delete-orphan"),
    )
    onebot_profile = db.relationship("OneBotProfile", backref=db.backref("bindings", lazy=True))


class AppSetting(db.Model):
    __tablename__ = "app_settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(255), default="")
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())


class BiliScreenshotTemplate(db.Model):
    __bind_key__ = "templates"
    __tablename__ = "bili_screenshot_templates"

    binding_id = db.Column(db.Integer, primary_key=True)
    template_dynamic = db.Column(db.Text, default="")
    template_live = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())


class BiliRuntimeStatus(db.Model):
    __bind_key__ = "status"
    __tablename__ = "bili_runtime_status"

    user_id = db.Column(db.Integer, primary_key=True)
    payload = db.Column(db.Text, default="")
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())


class BiliLogEntry(db.Model):
    __bind_key__ = "logs"
    __tablename__ = "bili_logs"

    id = db.Column(db.Integer, primary_key=True)
    time = db.Column(db.String(40), default="")
    level = db.Column(db.String(16), default="")
    logger = db.Column(db.String(120), default="")
    message = db.Column(db.Text, default="")
    uid = db.Column(db.String(32), default="")
    module = db.Column(db.String(120), default="")
    line = db.Column(db.Integer, default=0)
    thread = db.Column(db.String(120), default="")
    payload = db.Column(db.Text, default="")
