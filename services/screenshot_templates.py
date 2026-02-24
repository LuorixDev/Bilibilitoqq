import html as html_lib

DEFAULT_HTML_TEMPLATES = {
    "dynamic": """
<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <title>动态通知</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { width: 720px; height: auto; }
    body {
      font-family: \"Microsoft YaHei\", \"PingFang SC\", sans-serif;
      background: radial-gradient(circle at 10% 10%, rgba(120, 190, 255, 0.35), transparent 45%),
                  radial-gradient(circle at 90% 20%, rgba(150, 255, 210, 0.35), transparent 40%),
                  linear-gradient(135deg, #f4f8ff 0%, #f7fbff 60%, #f2f7ff 100%);
    }
    .card {
      background: rgba(255, 255, 255, 0.78);
      border-radius: 18px;
      box-shadow: 0 14px 28px rgba(32, 64, 96, 0.18);
      padding: 16px;
      border: 1px solid rgba(90, 140, 190, 0.18);
      backdrop-filter: blur(12px) saturate(140%);
    }
    .header { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
    .avatar-wrap {
      position: relative;
      width: 48px;
      height: 48px;
      flex: 0 0 auto;
    }
    .avatar {
      width: 48px; height: 48px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      background: linear-gradient(135deg, #5bbcff, #38c58f);
      color: #fff; font-weight: 700; font-size: 20px;
      position: relative;
      overflow: hidden;
    }
    .avatar-img {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: {avatar_display};
    }
    .avatar-initial {
      position: relative;
      z-index: 1;
      display: {avatar_text_display};
    }
    .meta { display: grid; gap: 4px; }
    .name { font-size: 18px; font-weight: 700; color: #1f2a37; }
    .title { font-size: 16px; color: #3b4a5a; }
    .text { margin-top: 6px; font-size: 15px; color: #2b3440; line-height: 1.5; }
    .text img { width: 18px; height: 18px; vertical-align: text-bottom; }
    .cover { margin-top: 12px; border-radius: 12px; width: 100%; display: {cover_display}; }
    .media { margin-top: 12px; display: grid; gap: 10px; }
    .media-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }
    .media-img { width: 100%; border-radius: 10px; object-fit: cover; }
    .media-card {
      display: grid;
      gap: 6px;
      padding: 10px;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.7);
      border: 1px solid rgba(120, 150, 190, 0.2);
      backdrop-filter: blur(8px) saturate(130%);
    }
    .forward-card {
      border-left: 3px solid rgba(91, 188, 255, 0.5);
      background: rgba(245, 250, 255, 0.85);
      padding: 10px;
      border-radius: 12px;
      display: grid;
      gap: 8px;
    }
    .forward-header { display: flex; gap: 8px; align-items: center; }
    .forward-avatar {
      width: 24px;
      height: 24px;
      border-radius: 50%;
      object-fit: cover;
      background: #e6edf5;
      flex: 0 0 auto;
    }
    .forward-info { display: grid; gap: 2px; }
    .forward-name { font-size: 12px; color: #1f2a37; font-weight: 600; }
    .forward-action { font-size: 11px; color: #5a6775; }
    .media-meta { font-size: 12px; color: #5a6775; }
    .media-stats { display: flex; gap: 12px; font-size: 12px; color: #5a6775; }
    .media-cover { width: 100%; border-radius: 10px; object-fit: cover; }
    .media-title { font-size: 14px; font-weight: 700; color: #1f2a37; }
    .media-desc { font-size: 12px; color: #5a6775; line-height: 1.4; }
    .media-link { font-size: 12px; color: #5b9bd5; word-break: break-all; }
    .media-author { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
    .media-author-avatar {
      width: 24px;
      height: 24px;
      border-radius: 50%;
      object-fit: cover;
      background: #e6edf5;
      flex: 0 0 auto;
    }
    .media-author-info { display: grid; gap: 2px; }
    .media-author-name { font-size: 12px; font-weight: 600; color: #1f2a37; }
    .media-author-action { font-size: 11px; color: #5a6775; }
    .link { margin-top: 10px; font-size: 12px; color: #5b9bd5; word-break: break-all; }
  </style>
</head>
<body>
  <div class=\"card\">
    <div class=\"header\">
      <div class=\"avatar-wrap\">
        <div class=\"avatar\">
          <img class=\"avatar-img\" src=\"{avatar}\" />
          <span class=\"avatar-initial\">{name_initial}</span>
        </div>
      </div>
      <div class=\"meta\">
        <div class=\"name\">{name}</div>
        <div class=\"title\">{title}</div>
      </div>
    </div>
    <div class=\"text\">{text_html}</div>
    <img class=\"cover\" src=\"{cover}\" />
    <div class=\"media\">{media_html}</div>
    <div class=\"link\">{url}</div>
  </div>
</body>
</html>
""",
    "live": """
<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <title>直播通知</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { width: 720px; height: auto; }
    body {
      font-family: \"Microsoft YaHei\", \"PingFang SC\", sans-serif;
      background: radial-gradient(circle at 15% 20%, rgba(120, 190, 255, 0.35), transparent 45%),
                  radial-gradient(circle at 85% 10%, rgba(150, 255, 210, 0.35), transparent 40%),
                  linear-gradient(135deg, #f4f8ff 0%, #f7fbff 60%, #f2f7ff 100%);
    }
    .card {
      background: rgba(255, 255, 255, 0.78);
      border-radius: 18px;
      box-shadow: 0 14px 28px rgba(32, 64, 96, 0.18);
      padding: 16px;
      border: 1px solid rgba(90, 140, 190, 0.18);
      backdrop-filter: blur(12px) saturate(140%);
    }
    .cover { border-radius: 12px; width: 100%; display: {cover_display}; }
    .live-header { display: flex; align-items: center; gap: 12px; margin-top: 12px; }
    .avatar-wrap {
      position: relative;
      width: 42px;
      height: 42px;
      flex: 0 0 auto;
    }
    .avatar {
      width: 42px; height: 42px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      background: linear-gradient(135deg, #5bbcff, #38c58f);
      color: #fff; font-weight: 700; font-size: 18px;
      position: relative; overflow: hidden;
    }
    .avatar-img {
      position: absolute; inset: 0; width: 100%; height: 100%;
      object-fit: cover; display: {avatar_display};
    }
    .avatar-initial { position: relative; z-index: 1; display: {avatar_text_display}; }
    .live-meta { display: grid; gap: 2px; }
    .live-name { font-size: 14px; font-weight: 700; color: #1f2a37; }
    .live-status { font-size: 12px; color: #5a6775; }
    .title { margin-top: 12px; font-size: 20px; font-weight: 700; color: #1f2a37; }
    .meta { margin-top: 6px; font-size: 14px; color: #5a6775; }
    .stats { margin-top: 10px; display: flex; gap: 12px; font-size: 14px; color: #2b3440; }
    .link { margin-top: 10px; font-size: 12px; color: #5b9bd5; word-break: break-all; }
  </style>
</head>
<body>
  <div class=\"card\">
    <img class=\"cover\" src=\"{cover}\" />
    <div class=\"title\">{title}</div>
    <div class=\"live-header\">
      <div class=\"avatar-wrap\">
        <div class=\"avatar\">
          <img class=\"avatar-img\" src=\"{avatar}\" />
          <span class=\"avatar-initial\">{name_initial}</span>
        </div>
      </div>
      <div class=\"live-meta\">
        <div class=\"live-name\">{name}</div>
        <div class=\"live-status\">正在直播</div>
      </div>
    </div>
    <div class=\"stats\">
      <div>人气：{online}</div>
      <div>时长：{duration}</div>
      <div>峰值：{max_online}</div>
    </div>
    <div class=\"link\">{url}</div>
  </div>
</body>
</html>
""",
}

HTML_TEMPLATE_VARS = [
    "{name}",
    "{name_initial}",
    "{text}",
    "{text_html}",
    "{title}",
    "{url}",
    "{online}",
    "{duration}",
    "{max_online}",
    "{cover}",
    "{cover_display}",
    "{avatar}",
    "{avatar_display}",
    "{avatar_text_display}",
    "{image_count}",
    "{media_html}",
]


def render_html_template(template: str, values: dict) -> str:
    html = template or ""
    for key, value in values.items():
        if key in ("media_html", "text_html"):
            safe = "" if value is None else str(value)
        else:
            safe = html_lib.escape("" if value is None else str(value))
        html = html.replace(f"{{{key}}}", safe)
    html = html.replace("{avatar_badge}", "")
    html = html.replace("{avatar_badge_display}", "none")
    return html
