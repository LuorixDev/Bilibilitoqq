import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# 基础配置（直接在本文件修改）
DATABASE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'data.db')}"
LOGS_DATABASE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'logs.db')}"
STATUS_DATABASE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'status.db')}"
TEMPLATES_DATABASE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'templates.db')}"
SECRET_KEY = "dev-secret-change-me"
POLL_INTERVAL = 30

# 管理员账号
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "change-me"
ADMIN_PASSWORD_HASH = ""

# B 站 API 配置
BILIBILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BILIBILI_COOKIE = ""
# bilibili-api-python 客户端设置
BILIBILI_HTTP_CLIENT = "curl_cffi"
BILIBILI_IMPERSONATE = "chrome131"
BILIBILI_PROXY = ""
# 账号凭据（可选，可直接填 Cookie 或单独填字段）
BILIBILI_SESSDATA = ""
BILIBILI_BILI_JCT = ""
BILIBILI_BUVID3 = ""
BILIBILI_BUVID4 = ""
BILIBILI_DEDEUSERID = ""
BILIBILI_AC_TIME_VALUE = ""
HTTP_TIMEOUT = 8
MAX_DYNAMIC_PER_POLL = 3
LIVE_HOURLY_INTERVAL = 3600

# 动态截图
DYNAMIC_SCREENSHOT_WAIT = 2.0
DYNAMIC_SCREENSHOT_FULL_PAGE = False

# HTML 截图模板
SCREENSHOT_TEMPLATE_PATH = os.path.join(BASE_DIR, "screenshot", "page", "0.html")
SCREENSHOT_WAIT = 0.3
SCREENSHOT_JPEG_QUALITY = 90

# 日志
LOG_FILE = os.path.join(BASE_DIR, "logs", "app.log")
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
