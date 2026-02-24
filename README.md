# bilibilitoqq

Flask + SQLite 的 B 站动态/直播监控面板，支持 OneBot 11 WS 推送与截图卡片。

## 功能
- 管理员后台添加、编辑、删除 UP 主
- 每个 UP 主支持多个 OneBot 绑定，独立配置目标群/私聊
- 动态更新推送（支持截图卡片）
- 视频更新推送（动态中的视频更新单独提示）
- 直播开播提醒
- 直播每小时播报当前信息
- 直播结束总结（时长、峰值人气）
- 调试日志页面（支持按等级/UID/关键字筛选）
- 刷新倒计时与检测间隔（全局默认 + 每个 UP 可单独设置）

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

复制配置模板并修改：

```bash
cp example.config.py config.py
```

如需截图功能，请安装 Playwright：

```bash
python -m playwright install chromium
```

启动：

```bash
python app.py
```

访问：`http://127.0.0.1:5000`

## 配置说明
配置文件：`config.py`

- `DATABASE_URL`：主库（用户/绑定/OneBot 配置）
- `LOGS_DATABASE_URL`：日志库
- `STATUS_DATABASE_URL`：运行状态库
- `TEMPLATES_DATABASE_URL`：截图模板库
- `SECRET_KEY`：Flask 密钥
- `POLL_INTERVAL`：全局默认轮询间隔（秒）
- `ADMIN_USERNAME` / `ADMIN_PASSWORD`：管理员账号密码
- `ADMIN_PASSWORD_HASH`：可选，使用 `werkzeug.security.generate_password_hash` 生成
- `BILIBILI_USER_AGENT`：请求头 User-Agent
- `BILIBILI_COOKIE`：可选，访问受限内容时使用（可自动解析账号字段）
- `BILIBILI_HTTP_CLIENT`：bilibili-api-python HTTP 客户端（默认 `curl_cffi`）
- `BILIBILI_IMPERSONATE`：curl_cffi 的浏览器指纹（默认 `chrome131`）
- `BILIBILI_PROXY`：代理（如 `http://127.0.0.1:7890`）
- `BILIBILI_SESSDATA` / `BILIBILI_BILI_JCT` / `BILIBILI_BUVID3` / `BILIBILI_BUVID4`：账号字段（可选）
- `MAX_DYNAMIC_PER_POLL`：单次轮询最多推送多少条动态
- `LIVE_HOURLY_INTERVAL`：直播播报间隔（秒）
- `DYNAMIC_SCREENSHOT_WAIT`：动态截图等待时间（秒）
- `SCREENSHOT_TEMPLATE_PATH` / `SCREENSHOT_WAIT`：截图模板与等待时间

OneBot 的地址、Token、目标群等均在“绑定管理”里为每个绑定单独配置。

## 使用说明
- 登录后台：`/login`
- 管理员在“UP 主管理”中添加 UP 主
- 在“绑定管理”中配置 OneBot WS 与通知选项
- “消息”页面可测试发送消息并查看回调结果
- “日志”页面可查看详细调试日志

## 截图模板
- 只有模板包含 `{SHOTPICTURE}` 时才会发送截图
- 默认模板已内置截图占位
- 可用变量：`{name} {text} {title} {url} {online} {duration} {max_online} {SHOTPICTURE} [atALL]`

## OneBot 说明
本项目作为 WebSocket 客户端主动连接 OneBot 11 服务端，请确保机器人框架已开启 WS 服务并可被访问。

常见示例：
- `ws://<host>:<port>`
- `ws://<host>:<port>?access_token=<token>`

## 数据与权限
- 管理员可修改全局默认检测间隔，以及每个 UP 的单独间隔
- UP 主只能查看自己的检测间隔
- 运行状态保存在 `status.db`，日志保存在 `logs.db`，截图模板保存在 `templates.db`

## 注意事项
- 默认仅适合内网或受控环境，若公开部署请增加安全防护
- 启动后首次轮询会先记录最新动态，避免历史推送
